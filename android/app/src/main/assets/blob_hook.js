/*
 * 导出下载桥（手机版专用）。
 *
 * 网页端导出简历用的是 URL.createObjectURL + a.click()。浏览器自己能处理 blob 下载，
 * 但 Android WebView 会把 blob: 当成普通下载转给系统 —— 系统进程读不到 WebView 里的
 * blob，结果就是「导出 PDF / 导出 Word」在手机版上完全没反应（或提示没有可打开的应用）。
 *
 * 这里钩住 HTMLAnchorElement.prototype.click：命中 blob 就把内容读成 base64 交给原生保存。
 * 只改 blob 这一种情况，其余点击原样透传，所以对正常导航零影响。
 */
(function () {
  if (window.__offerflowBlobHooked) {
    return;
  }
  window.__offerflowBlobHooked = true;

  function nativeBridge() {
    return window.OfferFlowNative || null;
  }

  function notify(message) {
    var bridge = nativeBridge();
    if (bridge && bridge.toast) {
      bridge.toast(message);
    }
  }

  function intercept(anchor) {
    var href = anchor.getAttribute ? anchor.getAttribute("href") : null;
    if (!href || href.indexOf("blob:") !== 0) {
      return false;
    }
    var bridge = nativeBridge();
    if (!bridge || !bridge.saveBase64) {
      return false;
    }
    var name = anchor.getAttribute("download") || "offerflow-download";

    window
      .fetch(href)
      .then(function (res) {
        var type = res.headers.get("Content-Type") || "";
        return res.blob().then(function (blob) {
          return { blob: blob, type: type };
        });
      })
      .then(function (payload) {
        var reader = new FileReader();
        reader.onload = function () {
          var dataUrl = String(reader.result || "");
          var comma = dataUrl.indexOf(",");
          bridge.saveBase64(
            name,
            payload.type || payload.blob.type || "application/octet-stream",
            comma >= 0 ? dataUrl.substring(comma + 1) : "",
          );
        };
        reader.onerror = function () {
          notify("读取导出内容失败");
        };
        reader.readAsDataURL(payload.blob);
      })
      .catch(function (err) {
        notify("导出失败：" + err);
      });
    return true;
  }

  var originalClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    try {
      if (intercept(this)) {
        return;
      }
    } catch (err) {
      /* 钩子自身出错就退回原生行为，绝不影响页面 */
    }
    return originalClick.apply(this, arguments);
  };
})();
