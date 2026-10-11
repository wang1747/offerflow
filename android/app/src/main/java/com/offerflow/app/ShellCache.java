package com.offerflow.app;

import android.content.Context;
import android.net.Uri;
import android.util.Log;
import android.webkit.WebResourceResponse;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.Charset;
import java.security.MessageDigest;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 离线外壳缓存。
 *
 * <p>为什么需要它：线上 {@code index.html} 为了「发版立刻生效」带的是
 * {@code Cache-Control: no-store}，所以断网时 WebView 的主框架请求必然失败、直接落到错误页；
 * 而 {@code /assets/*.js|css} 是 {@code public, immutable}（30 天），WebView 自己会缓存。
 * 于是本地只需额外留一份外壳文档 + 它引用的资源。
 *
 * <p>关键点：断网时不是换成 file:// 或本地 HTTP 服务，而是由
 * {@link android.webkit.WebViewClient#shouldInterceptRequest} 直接把这些字节喂回去。
 * 这样页面的**源仍然是站点 origin（BuildConfig.HOME_URL）** —— 前端 IndexedDB 里缓存的离线数据
 * 才读得到（换源就等于换了一个存储空间，数据全没了）。
 */
class ShellCache {

    private static final String TAG = "ShellCache";
    private static final Charset UTF8 = Charset.forName("UTF-8");
    /** 外壳文档的存储键后缀（实际键 = origin 的 sha1 + 此后缀） */
    private static final String SHELL_SUFFIX = "-shell";
    private static final Pattern ASSET_PATTERN =
            Pattern.compile("(?:src|href)=\"(/assets/[^\"]+)\"");

    private final File dir;
    /** 刷新守卫：onPageFinished 会连着触发，避免同一时刻跑两轮抓取 */
    private final AtomicBoolean refreshing = new AtomicBoolean(false);

    ShellCache(Context context) {
        this.dir = new File(context.getApplicationContext().getFilesDir(), "shell");
        if (!dir.exists() && !dir.mkdirs()) {
            Log.w(TAG, "无法创建外壳缓存目录: " + dir);
        }
    }

    // ------------------------------------------------------------ 路径判定

    /** 是不是「前端路由」——没有扩展名，且不是接口/下载/健康检查 */
    static boolean isRoutePath(String url) {
        Uri uri = Uri.parse(url);
        String path = uri.getPath() == null ? "/" : uri.getPath();
        if (path.startsWith("/api/") || path.startsWith("/downloads/") || path.startsWith("/health")) {
            return false;
        }
        String last = path.substring(path.lastIndexOf('/') + 1);
        return !last.contains(".");
    }

    private static boolean isAssetPath(String url) {
        Uri uri = Uri.parse(url);
        String path = uri.getPath() == null ? "" : uri.getPath();
        if (path.startsWith("/assets/")) {
            return true;
        }
        return path.endsWith(".js") || path.endsWith(".css");
    }

    // ------------------------------------------------------------ 读取

    /** 缓存里有就返回可直接喂给 WebView 的响应；没有返回 null（交回 WebView 正常处理） */
    WebResourceResponse serve(String url) {
        if (!isRoutePath(url) && !isAssetPath(url)) {
            return null;
        }
        File f = fileFor(isAssetPath(url) ? sha1(url) : shellKeyFor(url));
        if (!f.exists() || f.length() == 0L) {
            return null;
        }
        try {
            byte[] data = readAll(f);
            String mime = isAssetPath(url) ? mimeForAsset(url) : "text/html";
            return new WebResourceResponse(
                    mime, "utf-8", 200, "OK", headers(mime), new ByteArrayInputStream(data));
        } catch (Exception e) {
            Log.w(TAG, "读取外壳缓存失败 " + url + ": " + e);
            return null;
        }
    }

    /**
     * 该站点是否有可用的外壳副本。
     *
     * <p>按 origin 分桶：本地联调与线上的 HTML 各存一份，换站点时不会读到
     * 另一个站点的外壳 —— 否则断网会打开引用了不存在资源的旧 HTML（白屏）。
     */
    boolean hasShell(String url) {
        File f = fileFor(shellKeyFor(url));
        return f.exists() && f.length() > 0L;
    }

    private static String shellKeyFor(String url) {
        return sha1(originOf(url)) + SHELL_SUFFIX;
    }

    private static Map<String, String> headers(String mime) {
        Map<String, String> h = new HashMap<>();
        h.put("Content-Type", mime + "; charset=utf-8");
        return h;
    }

    private static String mimeForAsset(String url) {
        String path = Uri.parse(url).getPath();
        if (path == null) {
            return "application/octet-stream";
        }
        if (path.endsWith(".css")) {
            return "text/css";
        }
        if (path.endsWith(".js") || path.endsWith(".mjs")) {
            return "application/javascript";
        }
        return "application/octet-stream";
    }

    private File fileFor(String key) {
        return new File(dir, key);
    }

    // ------------------------------------------------------------ 刷新

    /**
     * 联网时刷新外壳缓存（后台线程）。
     *
     * <p>HTML 没变就只补下缺失的资源；变了就整份换掉并把新引用的资源补齐。
     * 全部失败都只记日志 —— 离线能力是「锦上添花」，绝不能影响正常使用。
     */
    void refreshAsync(final String baseUrl) {
        // 并发守卫：同一时刻只允许一轮抓取（重复触发直接忽略）
        if (!refreshing.compareAndSet(false, true)) {
            return;
        }
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    byte[] html = httpGet(baseUrl);
                    if (html == null || html.length == 0) {
                        return;
                    }
                    String text = new String(html, UTF8);
                    if (!text.contains("<div id=\"root\"")) {
                        // 拿到的不是应用外壳（可能是 nginx 错误页），不覆盖已有缓存
                        Log.w(TAG, "外壳内容异常，跳过刷新");
                        return;
                    }
                    File shellFile = fileFor(shellKeyFor(baseUrl));
                    String stored = readText(shellFile);
                    boolean changed = stored == null || !stored.equals(text);
                    if (changed) {
                        writeBytes(shellFile, html);
                    }
                    int fetched = downloadMissingAssets(text, baseUrl);
                    if (changed || fetched > 0) {
                        Log.i(TAG, "外壳缓存已刷新: changed=" + changed + " assets=" + fetched);
                    }
                } catch (Exception e) {
                    Log.w(TAG, "刷新外壳缓存失败: " + e);
                } finally {
                    refreshing.set(false);
                }
            }
        }, "shell-cache-refresh").start();
    }

    private int downloadMissingAssets(String html, String baseUrl) {
        Set<String> assets = new LinkedHashSet<>();
        Matcher m = ASSET_PATTERN.matcher(html);
        while (m.find()) {
            assets.add(m.group(1));
        }
        String origin = originOf(baseUrl);
        int fetched = 0;
        for (String path : assets) {
            File f = fileFor(sha1(origin + path));
            if (f.exists() && f.length() > 0L) {
                continue;
            }
            byte[] data = httpGet(origin + path);
            if (data != null && data.length > 0) {
                writeBytes(f, data);
                fetched++;
            }
        }
        return fetched;
    }

    // ------------------------------------------------------------ 工具

    private static String originOf(String url) {
        Uri uri = Uri.parse(url);
        String scheme = uri.getScheme();
        String authority = uri.getAuthority();
        if (scheme == null || authority == null) {
            return "";
        }
        return scheme + "://" + authority;
    }

    private static byte[] httpGet(String url) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(6000);
            conn.setReadTimeout(8000);
            // 明确要非压缩的字节：返回给 WebView 时我们不设置 Content-Encoding，
            // 拿到 gzip 内容会被当成乱码。
            conn.setRequestProperty("Accept-Encoding", "identity");
            conn.setRequestProperty("Cache-Control", "no-cache");
            if (conn.getResponseCode() != 200) {
                return null;
            }
            InputStream in = conn.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            in.close();
            return out.toByteArray();
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private static byte[] readAll(File f) throws Exception {
        FileInputStream in = new FileInputStream(f);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        in.close();
        return out.toByteArray();
    }

    private static String readText(File f) {
        try {
            if (!f.exists()) {
                return null;
            }
            return new String(readAll(f), UTF8);
        } catch (Exception e) {
            return null;
        }
    }

    private static void writeBytes(File f, byte[] data) {
        FileOutputStream out = null;
        try {
            out = new FileOutputStream(f);
            out.write(data);
        } catch (Exception e) {
            Log.w(TAG, "写外壳缓存失败 " + f.getName() + ": " + e);
        } finally {
            if (out != null) {
                try {
                    out.close();
                } catch (Exception ignored) {
                    // 关闭失败无所谓
                }
            }
        }
    }

    private static String sha1(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] digest = md.digest(s.getBytes(UTF8));
            StringBuilder sb = new StringBuilder();
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            return "k" + Integer.toHexString(s.hashCode());
        }
    }
}
