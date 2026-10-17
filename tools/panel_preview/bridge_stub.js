// AstrBot 插件页 bridge 的离线桩：只实现面板真正用到的接口，数据来自同目录 config.json。
(function () {
  var configPromise = null;
  function getConfig() {
    if (!configPromise) configPromise = fetch("./config.json").then(function (r) { return r.json(); });
    return configPromise;
  }
  window.AstrBotPluginPage = {
    ready: function () { return Promise.resolve(); },
    getContext: function () { return { locale: "zh-CN" }; },
    getLocale: function () { return "zh-CN"; },
    getI18n: function () { return {}; },
    t: function (key, fallback) { return fallback || key; },
    onContext: function () { return function () {}; },
    apiGet: function (endpoint, params) {
      if (endpoint === "config") return getConfig();
      if (endpoint === 'sessions/list') {
        return Promise.resolve({
          items: [
            { umo: "演示平台:GroupMessage:ABCDEF0123456789ABCDEF0123456789", label: "演示平台 · 群聊 ABCDEF0123456789ABCDEF0123456789" },
            { umo: "演示平台:FriendMessage:FEDCBA9876543210FEDCBA9876543210", label: "演示平台 · 私聊 FEDCBA9876543210FEDCBA9876543210" },
          ],
          platform_ids: ["演示平台"],
        });
      }
      if (endpoint === "joins") {
        return getConfig().then(function (cfg) {
          var gid = (params && params.group_id) || ((cfg.groups || [])[0] || {}).group_id || "g1";
          return {
            scope: {
              group_id: gid,
              follow_global: false,
              own_keys: ["join_llm_fallback_provider_ids"],
              restored_keys: [],
              overrides: { join_llm_fallback_provider_ids: ["gone/stale"] },
              overridable_keys: ((cfg.options || {}).join_overridable_keys) || [],
              global_only_keys: ((cfg.options || {}).join_global_only_keys) || []
            },
            pending: [],
            history: [],
            official: {}
          };
        });
      }
      return Promise.resolve({});
    },
    apiPost: function () { return Promise.resolve({ ok: true }); },
    upload: function () { return Promise.resolve({ path: "" }); },
    download: function () { return Promise.resolve(); },
    subscribeSSE: function () { return Promise.resolve("preview"); },
    unsubscribeSSE: function () { return Promise.resolve(); }
  };
})();
