package com.offerflow.app;

import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;

/**
 * 把内存里的字节存到用户找得到的地方。
 *
 * <p>为什么需要：网页端的「导出 PDF / 导出 Word / 导出用户 CSV」都是
 * {@code URL.createObjectURL} + {@code a.click()}。浏览器自己能处理 blob 下载，
 * 但 WebView 会把 {@code blob:} 当成普通下载交给系统 —— 系统浏览器打不开别的进程里的
 * blob，于是导出按钮在手机版上完全无效。这里由壳层接住并落盘。
 *
 * <p>落点选择：
 * <ul>
 *   <li>Android 10（API 29）及以上：写进系统「下载」集合（MediaStore），免任何权限；</li>
 *   <li>更早的系统：写进应用自己的外部目录。这条路径在 Android 9 及以下可以用文件管理器
 *       直接翻到，所以不申请 WRITE_EXTERNAL_STORAGE（少一个危险权限，用户少一次弹窗）。</li>
 * </ul>
 */
final class FileSaver {

    private FileSaver() {
    }

    static final class Result {
        final Uri uri;
        final String location;
        final String mime;

        Result(Uri uri, String location, String mime) {
            this.uri = uri;
            this.location = location;
            this.mime = mime;
        }
    }

    static Result save(Context context, String rawName, String mime, byte[] data) throws IOException {
        String name = safeName(rawName, mime);
        String type = (mime == null || mime.isEmpty())
                ? "application/octet-stream" : mime;

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return saveViaMediaStore(context, name, type, data);
        }
        return saveViaAppDir(context, name, data);
    }

    private static Result saveViaMediaStore(
            Context context, String name, String mime, byte[] data) throws IOException {
        ContentValues values = new ContentValues();
        values.put(MediaStore.Downloads.DISPLAY_NAME, name);
        values.put(MediaStore.Downloads.MIME_TYPE, mime);
        // IS_PENDING 期间对其他应用不可见：写完才"发布"，避免别的应用读到半个文件
        values.put(MediaStore.Downloads.IS_PENDING, 1);

        Uri uri = context.getContentResolver()
                .insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
        if (uri == null) {
            throw new IOException("系统下载目录不可用");
        }
        try (OutputStream out = context.getContentResolver().openOutputStream(uri)) {
            if (out == null) {
                throw new IOException("无法打开输出流");
            }
            out.write(data);
            out.flush();
        } catch (IOException e) {
            context.getContentResolver().delete(uri, null, null);
            throw e;
        }

        ContentValues done = new ContentValues();
        done.put(MediaStore.Downloads.IS_PENDING, 0);
        context.getContentResolver().update(uri, done, null, null);
        return new Result(uri, "下载/" + name, mime);
    }

    private static Result saveViaAppDir(Context context, String name, byte[] data)
            throws IOException {
        File dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        if (dir == null) {
            dir = context.getFilesDir();
        }
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IOException("无法创建保存目录");
        }
        File target = new File(dir, name);
        try (FileOutputStream out = new FileOutputStream(target)) {
            out.write(data);
            out.flush();
        }
        return new Result(Uri.fromFile(target), target.getAbsolutePath(), mimeOf(target.getName()));
    }

    /** 文件名清洗：去掉路径分隔符，补上扩展名，限制长度 */
    static String safeName(String rawName, String mime) {
        String name = rawName == null ? "" : rawName.trim();
        if (name.isEmpty()) {
            name = "offerflow-download";
        }
        name = name.replace('\\', '_').replace('/', '_')
                .replace(":", "_").replace("*", "_")
                .replace("?", "_").replace("\"", "_")
                .replace("<", "_").replace(">", "_").replace("|", "_");
        if (name.length() > 100) {
            name = name.substring(0, 100);
        }
        if (!name.contains(".")) {
            String ext = extensionFor(mime);
            if (ext != null) {
                name = name + "." + ext;
            }
        }
        return name;
    }

    /** 按扩展名推断 MIME（API 29 以下没有 MediaStore 帮我们记类型） */
    private static String mimeOf(String fileName) {
        String lower = fileName == null ? "" : fileName.toLowerCase();
        if (lower.endsWith(".pdf")) {
            return "application/pdf";
        }
        if (lower.endsWith(".docx")) {
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
        }
        if (lower.endsWith(".csv")) {
            return "text/csv";
        }
        if (lower.endsWith(".txt")) {
            return "text/plain";
        }
        return "application/octet-stream";
    }

    private static String extensionFor(String mime) {
        if (mime == null) {
            return null;
        }
        switch (mime) {
            case "application/pdf":
                return "pdf";
            case "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                return "docx";
            case "text/csv":
                return "csv";
            case "application/zip":
                return "zip";
            case "text/plain":
                return "txt";
            case "image/png":
                return "png";
            case "image/jpeg":
                return "jpg";
            default:
                return null;
        }
    }
}
