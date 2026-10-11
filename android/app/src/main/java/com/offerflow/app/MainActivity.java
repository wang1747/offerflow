package com.offerflow.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * OfferFlow 手机版外壳。
 *
 * <p>设计原则与电脑版一致：**功能只有一份，在网页端**。这个 APK 只做四件事：
 * <ol>
 *   <li>用一个全屏 WebView 承载线上站点（网页一发版，手机端立刻同步，无需重新装包）</li>
 *   <li>补齐原生能力：文件选择（JD 截图上传）、导出文件落盘、外链跳系统浏览器、返回键</li>
 *   <li>断网兜底：外壳与资源留本地副本，断网也能进得去（见 {@link ShellCache}）</li>
 *   <li>提供「检查更新」入口，让用户能在应用内拿到新安装包</li>
 * </ol>
 */
public class MainActivity extends Activity {

    private static final String TAG = "OfferFlow";
    private static final int REQ_FILE_CHOOSER = 1001;
    private static final long EXIT_INTERVAL_MS = 2000L;

    private WebView web;
    private ProgressBar progressBar;
    private View splashView;
    private View errorView;
    private TextView errorDesc;

    private ValueCallback<Uri[]> filePathCallback;
    private long lastBackPressedAt = 0L;

    private ShellCache shellCache;
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;

    /** 断网兜底被触发过：此时所有外壳资源都走本地副本 */
    private volatile boolean forceOffline = false;
    /** 主框架失败后的"改用缓存重试"只做一次，避免死循环 */
    private boolean offlineReloadTried = false;
    /** blob 下载钩子脚本（从 assets 读一次就缓存） */
    private String blobHookScript;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler handler = new Handler(Looper.getMainLooper());

    /**
     * 站点主机名（用于判定站内 / 站外链接）。
     * 从构建期配置的 {@link BuildConfig#HOME_URL} 派生，避免同一地址在两处各写一份。
     */
    private String siteHost = Uri.parse(BuildConfig.HOME_URL).getHost();

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        web = findViewById(R.id.web_view);
        progressBar = findViewById(R.id.progress_bar);
        splashView = findViewById(R.id.splash_view);
        errorView = findViewById(R.id.error_view);
        errorDesc = findViewById(R.id.error_desc);

        shellCache = new ShellCache(this);

        Button retry = findViewById(R.id.btn_retry);
        retry.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                // 手动重试要放弃"离线模式"，重新按在线流程走一遍
                forceOffline = false;
                offlineReloadTried = false;
                errorView.setVisibility(View.GONE);
                web.setVisibility(View.VISIBLE);
                web.loadUrl(BuildConfig.HOME_URL);
            }
        });

        Uri home = Uri.parse(BuildConfig.HOME_URL);
        if (home.getHost() != null) {
            siteHost = home.getHost();
        }

        configureWebView();
        registerNetworkCallback();

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
        } else {
            web.loadUrl(BuildConfig.HOME_URL);
        }

        // 启动后延迟检查更新，避免和首屏加载抢网络
        handler.postDelayed(new Runnable() {
            @Override
            public void run() {
                checkUpdate();
            }
        }, 3500);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        // 登录态与前端本地状态都依赖 DOM Storage（localStorage）
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setSupportZoom(false);
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setTextZoom(100);
        // 站点仍是 HTTP，必须放行混合内容，否则图片 / 接口会被拦
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }
        // 让服务端能识别出「这是手机版客户端」，便于后续做版本统计
        String ua = s.getUserAgentString();
        if (ua != null && !ua.contains("OfferFlowApp")) {
            s.setUserAgentString(ua + " OfferFlowApp/" + BuildConfig.VERSION_NAME);
        }

        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookieManager.setAcceptThirdPartyCookies(web, true);
        }

        // 导出（blob 下载）必须由原生接住，见 assets/blob_hook.js
        web.addJavascriptInterface(new NativeBridge(), "OfferFlowNative");

        web.setWebViewClient(new WebViewClient() {

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                return handleUri(uri) == Handled.EXTERNAL;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleUri(Uri.parse(url)) == Handled.EXTERNAL;
            }

            /**
             * 断网时把外壳文档与 assets 直接从本地副本喂回去。
             *
             * <p>刻意只在「离线」时介入：在线时一律返回 null 让 WebView 正常走网络，
             * 这样发版能立刻生效、也不会绕开 WebView 自己的 HTTP 缓存。
             */
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                try {
                    if (request == null || request.getUrl() == null) {
                        return null;
                    }
                    if (!"GET".equalsIgnoreCase(request.getMethod())) {
                        return null;
                    }
                    if (!isSameOrigin(request.getUrl())) {
                        return null;
                    }
                    if (!offlineActive()) {
                        return null;
                    }
                    return shellCache.serve(request.getUrl().toString());
                } catch (Exception e) {
                    Log.w(TAG, "shouldInterceptRequest 异常: " + e);
                    return null;
                }
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                progressBar.setVisibility(View.VISIBLE);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                progressBar.setVisibility(View.GONE);
                hideSplash();
                offlineReloadTried = false;
                injectBlobHook();
                // 联网时顺手把外壳副本刷新一下（后台线程，失败只记日志）
                if (!forceOffline && networkAvailable()) {
                    shellCache.refreshAsync(BuildConfig.HOME_URL);
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                // 只对主文档失败做兜底，子资源失败（图片、埋点）不该整页弹错误
                if (request != null && request.isForMainFrame()) {
                    handleMainFrameError();
                }
            }

            @Override
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                if (failingUrl != null && failingUrl.equals(view.getUrl())) {
                    handleMainFrameError();
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progressBar.setProgress(newProgress);
                if (newProgress >= 100) {
                    progressBar.setVisibility(View.GONE);
                    hideSplash();
                }
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                // 网页端的「上传 JD 截图 / 上传简历」走这里
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = callback;
                try {
                    Intent intent = params.createIntent();
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    startActivityForResult(intent, REQ_FILE_CHOOSER);
                    return true;
                } catch (Exception e) {
                    filePathCallback = null;
                    toast("无法打开文件选择器");
                    return false;
                }
            }
        });

        // 下载类链接（APK / 桌面版 zip）交给系统浏览器，安装与文件管理更顺
        web.setDownloadListener(new DownloadListener() {
            @Override
            public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                if (url != null && url.startsWith("blob:")) {
                    // 正常情况下 blob 已经被注入的钩子接住了；走到这里说明钩子没装上，
                    // 补装一次请用户重试，而不是把它丢给系统（系统读不到这个 blob）。
                    injectBlobHook();
                    toast(getString(R.string.hook_retry));
                    return;
                }
                openExternal(Uri.parse(url));
            }
        });
    }

    // ------------------------------------------------------------ 离线判定

    private boolean isSameOrigin(Uri uri) {
        String host = uri.getHost() == null ? "" : uri.getHost().toLowerCase();
        return host.equals(siteHost);
    }

    private boolean offlineActive() {
        return forceOffline || !networkAvailable();
    }

    private boolean networkAvailable() {
        try {
            ConnectivityManager cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return true;
            }
            Network network = cm.getActiveNetwork();
            if (network == null) {
                return false;
            }
            NetworkCapabilities caps = cm.getNetworkCapabilities(network);
            return caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        } catch (Exception e) {
            return true;
        }
    }

    private void registerNetworkCallback() {
        try {
            connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (connectivityManager == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.N) {
                return;
            }
            networkCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    if (forceOffline) {
                        forceOffline = false;
                        handler.post(new Runnable() {
                            @Override
                            public void run() {
                                toast(getString(R.string.network_back));
                            }
                        });
                    }
                }
            };
            connectivityManager.registerDefaultNetworkCallback(networkCallback);
        } catch (Exception e) {
            Log.w(TAG, "注册网络回调失败: " + e);
        }
    }

    /** 主框架加载失败：有本地副本就改用副本重试一次，否则才是真的打不开 */
    private void handleMainFrameError() {
        if (!offlineReloadTried && shellCache.hasShell(BuildConfig.HOME_URL)) {
            offlineReloadTried = true;
            forceOffline = true;
            toast(getString(R.string.offline_fallback));
            web.loadUrl(BuildConfig.HOME_URL);
            return;
        }
        progressBar.setVisibility(View.GONE);
        splashView.setVisibility(View.GONE);
        errorDesc.setText(getString(R.string.error_desc));
        errorView.setVisibility(View.VISIBLE);
    }

    // ------------------------------------------------------------ blob 下载桥

    private void injectBlobHook() {
        try {
            if (blobHookScript == null) {
                blobHookScript = readAsset("blob_hook.js");
            }
            if (blobHookScript == null || blobHookScript.isEmpty()) {
                return;
            }
            web.evaluateJavascript(blobHookScript, null);
        } catch (Exception e) {
            Log.w(TAG, "注入导出钩子失败: " + e);
        }
    }

    private String readAsset(String name) {
        InputStream in = null;
        try {
            in = getAssets().open(name);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            return out.toString("UTF-8");
        } catch (Exception e) {
            Log.w(TAG, "读取 " + name + " 失败: " + e);
            return null;
        } finally {
            if (in != null) {
                try {
                    in.close();
                } catch (Exception ignored) {
                    // 忽略
                }
            }
        }
    }

    /** 暴露给网页的桥（只给本应用加载的站点用） */
    private class NativeBridge {

        @JavascriptInterface
        public void toast(final String message) {
            handler.post(new Runnable() {
                @Override
                public void run() {
                    MainActivity.this.toast(message);
                }
            });
        }

        @JavascriptInterface
        public void saveBase64(final String name, final String mime, final String base64) {
            // 这个回调跑在 WebView 的 JS 线程上：解码与落盘都挪到后台，避免卡住页面
            executor.execute(new Runnable() {
                @Override
                public void run() {
                    String failure = null;
                    FileSaver.Result result = null;
                    try {
                        byte[] data = Base64.decode(base64, Base64.DEFAULT);
                        result = FileSaver.save(MainActivity.this, name, mime, data);
                    } catch (Exception e) {
                        failure = e.getMessage() == null ? e.toString() : e.getMessage();
                        Log.w(TAG, "保存导出文件失败: " + e);
                    }
                    final String error = failure;
                    final FileSaver.Result saved = result;
                    handler.post(new Runnable() {
                        @Override
                        public void run() {
                            if (saved == null) {
                                toast(getString(R.string.save_failed, error == null ? "未知错误" : error));
                                return;
                            }
                            toast(getString(R.string.saved_to, saved.location));
                            openSavedFile(saved);
                        }
                    });
                }
            });
        }
    }

    /** 存完顺手打开一次：能打开就直接看，打不开也已经存在「下载」里了 */
    private void openSavedFile(FileSaver.Result saved) {
        if (saved.uri == null) {
            return;
        }
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW);
            intent.setDataAndType(saved.uri, saved.mime == null ? "*/*" : saved.mime);
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (intent.resolveActivity(getPackageManager()) != null) {
                startActivity(intent);
            }
        } catch (Exception e) {
            // 打不开不算错误，文件已经保存好了
            Log.i(TAG, "没有可用应用打开导出文件: " + e);
        }
    }

    // ------------------------------------------------------------ 链接与视图

    private enum Handled { INTERNAL, EXTERNAL }

    /** 决定一个 URL 由 WebView 自己打开，还是交给系统处理。 */
    private Handled handleUri(Uri uri) {
        if (uri == null) {
            return Handled.INTERNAL;
        }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase();
        if (!scheme.equals("http") && !scheme.equals("https")) {
            // tel: mailto: weixin: intent: 等
            openExternal(uri);
            return Handled.EXTERNAL;
        }
        String host = uri.getHost() == null ? "" : uri.getHost().toLowerCase();
        if (host.equals(siteHost)) {
            String path = uri.getPath() == null ? "" : uri.getPath();
            if (path.startsWith("/downloads/")) {
                openExternal(uri);
                return Handled.EXTERNAL;
            }
            return Handled.INTERNAL;
        }
        openExternal(uri);
        return Handled.EXTERNAL;
    }

    private void openExternal(Uri uri) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, uri);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (ActivityNotFoundException e) {
            toast("没有可打开该链接的应用");
        }
    }

    private void hideSplash() {
        if (splashView.getVisibility() == View.VISIBLE) {
            splashView.setVisibility(View.GONE);
        }
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQ_FILE_CHOOSER) {
            if (filePathCallback == null) {
                super.onActivityResult(requestCode, resultCode, data);
                return;
            }
            Uri[] results = null;
            if (resultCode == RESULT_OK && data != null) {
                if (data.getClipData() != null) {
                    int count = data.getClipData().getItemCount();
                    results = new Uri[count];
                    for (int i = 0; i < count; i++) {
                        results[i] = data.getClipData().getItemAt(i).getUri();
                    }
                } else if (data.getData() != null) {
                    results = new Uri[]{data.getData()};
                }
            }
            filePathCallback.onReceiveValue(results);
            filePathCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            if (errorView.getVisibility() == View.VISIBLE) {
                // 错误页上按返回直接退出，不然用户会觉得"退不出去"
                finish();
                return true;
            }
            if (web.canGoBack()) {
                web.goBack();
                return true;
            }
            long now = System.currentTimeMillis();
            if (now - lastBackPressedAt < EXIT_INTERVAL_MS) {
                finish();
            } else {
                lastBackPressedAt = now;
                toast(getString(R.string.exit_hint));
            }
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (web != null) {
            web.onPause();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) {
                // 已注销过就忽略
            }
            networkCallback = null;
        }
        executor.shutdownNow();
        if (web != null) {
            web.destroy();
        }
        super.onDestroy();
    }

    // ---------------------- 应用内检查更新 ----------------------

    private void checkUpdate() {
        executor.execute(new Runnable() {
            @Override
            public void run() {
                final JSONObject manifest = fetchManifest();
                if (manifest == null) {
                    return;
                }
                final String latest = manifest.optString("versionName", "");
                if (latest.isEmpty() || !isNewer(latest, BuildConfig.VERSION_NAME)) {
                    return;
                }
                final SharedPreferences sp = getSharedPreferences("offerflow", MODE_PRIVATE);
                if (latest.equals(sp.getString("skippedVersion", ""))) {
                    return;
                }
                final String notes = manifest.optString("notes", "");
                final String url = manifest.optString("downloadUrl", BuildConfig.APK_URL);
                handler.post(new Runnable() {
                    @Override
                    public void run() {
                        showUpdateDialog(latest, notes, url);
                    }
                });
            }
        });
    }

    private JSONObject fetchManifest() {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(BuildConfig.UPDATE_MANIFEST).openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(8000);
            conn.setRequestProperty("Cache-Control", "no-cache");
            StringBuilder sb = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), "UTF-8"))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    sb.append(line);
                }
            }
            return new JSONObject(sb.toString());
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private void showUpdateDialog(final String version, final String notes, final String url) {
        if (isFinishing()) {
            return;
        }
        StringBuilder msg = new StringBuilder("版本 " + version);
        if (!notes.isEmpty()) {
            msg.append("\n\n").append(notes);
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.update_title)
                .setMessage(msg.toString())
                .setPositiveButton(R.string.update_now, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        openExternal(Uri.parse(url));
                    }
                })
                .setNegativeButton(R.string.update_later, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface dialog, int which) {
                        getSharedPreferences("offerflow", MODE_PRIVATE)
                                .edit()
                                .putString("skippedVersion", version)
                                .apply();
                    }
                })
                .show();
    }

    /** 语义化版本比较：1.0.10 > 1.0.9 */
    private static boolean isNewer(String candidate, String current) {
        int[] a = parseVersion(candidate);
        int[] b = parseVersion(current);
        int len = Math.max(a.length, b.length);
        for (int i = 0; i < len; i++) {
            int x = i < a.length ? a[i] : 0;
            int y = i < b.length ? b[i] : 0;
            if (x != y) {
                return x > y;
            }
        }
        return false;
    }

    private static int[] parseVersion(String v) {
        String cleaned = v == null ? "" : v.trim().replaceAll("^[vV]", "");
        String[] parts = cleaned.split("\\.");
        int[] out = new int[parts.length];
        for (int i = 0; i < parts.length; i++) {
            String digits = parts[i].replaceAll("[^0-9].*$", "");
            try {
                out[i] = digits.isEmpty() ? 0 : Integer.parseInt(digits);
            } catch (NumberFormatException e) {
                out[i] = 0;
            }
        }
        return out;
    }
}
