# OfferFlow 手机版（Android）

> 一个**极简 WebView 外壳**：加载网页版，在线时所有功能都走网络。

## 设计原则：功能只有一份，在网页端

壳本身不实现任何业务逻辑。在线时 `shouldInterceptRequest` 直接返回 `null` 走真实网络请求，所以**网页版一发版，手机端立刻就是最新**，壳不用跟着重发。

壳只做四件事：

1. **加载站点**（`BuildConfig.HOME_URL`）
2. **断网兜底**：把已缓存的外壳文档 + 资源直接喂回 WebView，且**保持原始 origin**（换源会导致 IndexedDB 里的离线数据读不到）
3. **文件保存**：处理页面里的 blob 下载（导出简历等）
4. **检查更新**：读站点上的 `android.json` 版本清单，提示下载新 APK

刻意**不引入任何第三方依赖**（连 AndroidX 都没有），全部用框架原生 API——安装包只有几百 KB，也不会有依赖版本冲突。

## 构建

```bash
# 1. 指向 Android SDK
echo "sdk.dir=/path/to/Android/Sdk" > local.properties

# 2. 配置（可选）
cp gradle.properties.example gradle.properties

# 3. 构建
gradle assembleRelease
# 产物：app/build/outputs/apk/release/app-release.apk
```

需要 **JDK 17** + **Android SDK 34**。

### 指定站点地址

```bash
gradle assembleRelease -POFFERFLOW_HOME_URL=http://your-host/
```

默认 `http://10.0.2.2:8001/`（`10.0.2.2` 是 Android 模拟器里指向宿主机 localhost 的固定地址，正好对应网页版后端的 `:8001`）。

### 签名

仓库**不包含**签名私钥。没有 `keystore/offerflow.jks` 时只产出未签名包；自己发布请生成 keystore 并填 `gradle.properties` 里那三项口令。

## 目录

```
android/
├── app/build.gradle                     # 版本号 / 站点地址 / 签名
├── app/src/main/java/com/offerflow/app/
│   ├── MainActivity.java                # WebView 主体、站内外链接判定、检查更新
│   ├── ShellCache.java                  # 断网兜底（保持 origin 的字节回喂）
│   └── FileSaver.java                   # 下载保存
└── app/src/main/assets/blob_hook.js     # 页面 blob 下载钩子
```
