// 渲染级验证用：在页面脚本之前注入。
// 1) 收集 JS 运行时错误；2) 把错误**写进 DOM**，这样 chrome --dump-dom 也能看见（dump-dom 拿不到 console）。
window.__errs = [];
function __flush() {
  try {
    var box = document.getElementById("__errhook");
    if (!box) {
      box = document.createElement("div");
      box.id = "__errhook";
      box.style.display = "none";
      (document.body || document.documentElement).appendChild(box);
    }
    box.textContent = window.__errs.join(" || ");
  } catch (e) { /* 忽略 */ }
}
window.addEventListener("error", function (e) {
  window.__errs.push(String(e.message || e.error));
  __flush();
});
window.addEventListener("unhandledrejection", function (e) {
  window.__errs.push("rej:" + String(e.reason));
  __flush();
});
// 每 200ms 刷一次：捕获渲染后（异步）才发生的错误
setInterval(__flush, 200);
