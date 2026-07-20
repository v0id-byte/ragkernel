window.RK_I18N = (function () {
  var STORAGE_KEY = 'rk_lang';

  var DICT = {
    zh: {
      'meta.titleIndex': 'ragkernel · 企业知识库',
      'meta.titleAdmin': 'ragkernel · 后台管理',
      'nav.langToggle': '切换语言',
      'nav.docsTitle': '文档',
      'nav.photoTitle': '拍照/上传故障照片（错误屏 / 铭牌）',
      'nav.closeTitle': '关闭',
      'nav.dashboard': '仪表盘',
      'nav.logout': '退出',
      'header.subFull': '企业知识库 · 本地检索 · 带引用问答',

      'login.subIndex': '企业知识库 · 登录后可提问',
      'login.subAdmin': '后台管理 · 管理员登录',
      'login.usernamePlaceholder': '用户名',
      'login.continue': '继续',
      'login.accountLabel': '账号：',
      'login.changeAccount': '换一个',
      'login.passwordPlaceholder': '密码',
      'login.loginBtn': '登录',
      'login.setupSubIndex': '首次登录，请输入管理员给你的建号口令并设置密码',
      'login.setupSubAdmin': '首次登录，请输入建号口令并设置密码',
      'login.setupCodePlaceholder': '建号口令',
      'login.setupPasswordPlaceholder': '设置密码（至少 6 位）',
      'login.setupBtn': '设置密码并登录',
      'login.expired': '登录已过期，请重新登录',
      'login.requestFailed': '请求失败',
      'login.notExist': '账号不存在，请联系管理员建号',
      'login.loggingIn': '登录中…',
      'login.loginFailed': '登录失败',
      'login.passwordMinLen': '密码至少 6 位',
      'login.settingUp': '设置中…',
      'login.setupFailed': '设置失败',
      'login.sessionExpiredAdmin': '会话已失效，请重新登录',
      'login.sessionCreateFailed': '会话建立失败，请重新登录',

      'docs.title': '文档',
      'docs.dropText': '拖拽文件到此，或点击选择',
      'docs.dropSub': 'PDF · Word · PPT · Markdown · CSV/Excel 工单，自动索引',
      'docs.countSuffix': ' 篇',
      'docs.chunkSuffix': ' 片',
      'docs.pageSuffix': ' 页',
      'docs.deleteTitle': '删除',
      'docs.kind.upload': '上传',
      'docs.kind.ticket_import': '工单导入',
      'docs.kind.feedback': '反馈案例',
      'docs.uploadingPrefix': '上传中 ',
      'docs.uploadFailedPrefix': '未成功 · ',
      'docs.stage.received': '已接收',
      'docs.stage.loading': '解析中',
      'docs.stage.chunking': '分片中',
      'docs.stage.embedding': '向量化（模型预热可能稍慢）',
      'docs.stage.chunked': '分片完成',
      'docs.stage.done': '已索引',
      'docs.stage.skip': '已索引过（跳过）',

      'chat.inputPlaceholder': '向知识库提问……可附故障照片',
      'chat.send': '提问',
      'chat.thinking': '思考中…',
      'chat.photoQuestionFallback': '照片提问',
      'chat.hintHtml': '上传手册或工单后即可提问。答案只依据文档内容，并附 <code>[D&lt;文档&gt;#&lt;块&gt; p.&lt;页&gt;]</code> 引用；查不到会如实说明。答完可记录处理结果，回填知识库。',
      'cites.more': '+{{n}} 处引用',
      'cites.collapse': '收起',

      'feedback.recordButton': '记录处理结果',
      'feedback.equipmentPlaceholder': '设备 / 型号（选填）',
      'feedback.resultPlaceholder': '结果（选填）',
      'feedback.questionPlaceholder': '故障现象 / 问题',
      'feedback.resolutionPlaceholder': '实际处理 / 解决办法（必填）',
      'feedback.submit': '入库',
      'feedback.mustFillResolution': '请填写实际处理',
      'feedback.saving': '入库中…',
      'feedback.savedDone': '已入库为 D{{docId}}「{{category}}」，下次同类问题会用上。',

      'dash.title': '知识库仪表盘',
      'dash.sub': '文件 · 片段 · 索引 · 提问 —— 均有记录',
      'dash.card.documents': '文档',
      'dash.card.chunks': '片段',
      'dash.card.embedded': '已索引',
      'dash.card.chars': '字符数',
      'dash.card.queries': '提问数',
      'dash.card.sessions': '会话数',
      'dash.bySource': '来源构成（含反馈回填）',
      'dash.categoryDist': '分类分布',
      'dash.days14': '近 14 天提问量',
      'dash.empty': '暂无',
      'dash.ingestionHistory': '摄取历史',

      'admin.headerSub': '后台管理',
      'admin.noperm': '当前账号不是管理员，无权限访问。',
      'admin.provider.title': 'AI 服务提供方',
      'admin.provider.sub': '配置用来生成回答的模型接口——可以填云端 API（Claude / MiniMax 等），也可以填一个已经在跑的本地推理服务地址（Ollama / vLLM，OpenAI 兼容）。改完立即生效，不用重启。',
      'admin.provider.kindAnthropic': 'Anthropic 兼容（Claude / MiniMax）',
      'admin.provider.kindOpenai': 'OpenAI 兼容（vLLM / Ollama / 本地服务）',
      'admin.provider.baseUrlPlaceholder': '接口地址 base_url（本地服务示例：http://localhost:11434/v1）',
      'admin.provider.modelPlaceholder': '模型名称，如 MiniMax-M3 / claude-sonnet-5 / qwen2.5:14b',
      'admin.provider.apiKeyPlaceholder': 'API Key（本地服务通常不需要）',
      'admin.provider.maxTokenPlaceholder': '单次最大 token',
      'admin.provider.testBtn': '测试连接',
      'admin.provider.resetBtn': '恢复默认',
      'admin.provider.saveBtn': '保存',
      'admin.provider.keyHintSet': '当前已配置 API Key（{{hint}}）。这里留空保存 = 不修改它。',
      'admin.provider.keyHintUnset': '尚未配置 API Key（本地服务通常不需要）。',
      'admin.provider.saving': '保存中…',
      'admin.provider.saveFailed': '保存失败',
      'admin.provider.saved': '已保存，立即生效。',
      'admin.provider.testing': '测试中…',
      'admin.provider.testFailedPrefix': '连接失败：',
      'admin.provider.testSuccessPrefix': '连接成功，模型回复：',
      'admin.provider.testEmptyReply': '（空）',
      'admin.provider.resetting': '恢复中…',
      'admin.provider.resetFailed': '恢复失败',
      'admin.provider.resetDone': '已恢复为 config/settings.yaml + .env 的默认值。',

      'admin.users.title': '新建用户',
      'admin.users.usernamePlaceholder': '用户名',
      'admin.users.initialPasswordPlaceholder': '初始密码（留空=生成建号口令）',
      'admin.users.adminLabel': '管理员',
      'admin.users.createBtn': '新建',
      'admin.users.usernameRequired': '用户名不能为空',
      'admin.users.createFailed': '新建失败',
      'admin.users.createdResult': '已建待激活账号 {{username}}，建号口令：{{setupCode}}（交给本人，只显示这一次）',
      'admin.users.listTitle': '用户列表',
      'admin.users.colUser': '用户',
      'admin.users.colStatus': '状态',
      'admin.users.colCreated': '创建时间',
      'admin.users.tagAdmin': '管理员',
      'admin.users.tagDisabled': '已禁用',
      'admin.users.tagPending': '待激活',
      'admin.users.statusActive': '正常',
      'admin.users.deactivate': '禁用',
      'admin.users.activate': '启用',
    },
    en: {
      'meta.titleIndex': 'ragkernel · Enterprise Knowledge Base',
      'meta.titleAdmin': 'ragkernel · Admin Console',
      'nav.langToggle': 'Switch language',
      'nav.docsTitle': 'Documents',
      'nav.photoTitle': 'Attach a photo (error screen / nameplate)',
      'nav.closeTitle': 'Close',
      'nav.dashboard': 'Dashboard',
      'nav.logout': 'Log out',
      'header.subFull': 'Enterprise knowledge base · local retrieval · cited answers',

      'login.subIndex': 'Enterprise knowledge base · log in to ask questions',
      'login.subAdmin': 'Admin console · administrator login',
      'login.usernamePlaceholder': 'Username',
      'login.continue': 'Continue',
      'login.accountLabel': 'Account: ',
      'login.changeAccount': 'switch account',
      'login.passwordPlaceholder': 'Password',
      'login.loginBtn': 'Log in',
      'login.setupSubIndex': 'First login — enter the setup code your admin gave you and set a password',
      'login.setupSubAdmin': 'First login — enter the setup code and set a password',
      'login.setupCodePlaceholder': 'Setup code',
      'login.setupPasswordPlaceholder': 'Set a password (6+ characters)',
      'login.setupBtn': 'Set password & log in',
      'login.expired': 'Session expired, please log in again',
      'login.requestFailed': 'Request failed',
      'login.notExist': 'Account not found — ask an admin to create one',
      'login.loggingIn': 'Logging in…',
      'login.loginFailed': 'Login failed',
      'login.passwordMinLen': 'Password must be at least 6 characters',
      'login.settingUp': 'Setting up…',
      'login.setupFailed': 'Setup failed',
      'login.sessionExpiredAdmin': 'Session expired, please log in again',
      'login.sessionCreateFailed': 'Failed to establish a session, please log in again',

      'docs.title': 'Documents',
      'docs.dropText': 'Drag files here, or click to choose',
      'docs.dropSub': 'PDF · Word · PPT · Markdown · CSV/Excel tickets, auto-indexed',
      'docs.countSuffix': '',
      'docs.chunkSuffix': ' chunks',
      'docs.pageSuffix': ' pages',
      'docs.deleteTitle': 'Delete',
      'docs.kind.upload': 'upload',
      'docs.kind.ticket_import': 'ticket import',
      'docs.kind.feedback': 'feedback case',
      'docs.uploadingPrefix': 'Uploading ',
      'docs.uploadFailedPrefix': 'Failed · ',
      'docs.stage.received': 'Received',
      'docs.stage.loading': 'Parsing',
      'docs.stage.chunking': 'Chunking',
      'docs.stage.embedding': 'Embedding (model warm-up may be slow)',
      'docs.stage.chunked': 'Chunked',
      'docs.stage.done': 'Indexed',
      'docs.stage.skip': 'Already indexed (skipped)',

      'chat.inputPlaceholder': 'Ask the knowledge base… you can attach a photo',
      'chat.send': 'Ask',
      'chat.thinking': 'Thinking…',
      'chat.photoQuestionFallback': 'Photo question',
      'chat.hintHtml': 'Upload manuals or tickets, then ask. Answers are grounded only in document content, with <code>[D&lt;doc&gt;#&lt;chunk&gt; p.&lt;page&gt;]</code> citations; if nothing is found, it will say so honestly. After answering, you can record the resolution to feed back into the knowledge base.',
      'cites.more': '+{{n}} more',
      'cites.collapse': 'Collapse',

      'feedback.recordButton': 'Record resolution',
      'feedback.equipmentPlaceholder': 'Equipment / model (optional)',
      'feedback.resultPlaceholder': 'Result (optional)',
      'feedback.questionPlaceholder': 'Symptom / problem',
      'feedback.resolutionPlaceholder': 'What was actually done / the fix (required)',
      'feedback.submit': 'Save',
      'feedback.mustFillResolution': 'Please fill in the resolution',
      'feedback.saving': 'Saving…',
      'feedback.savedDone': 'Saved as D{{docId}} "{{category}}" — it will be used for similar questions next time.',

      'dash.title': 'Knowledge Base Dashboard',
      'dash.sub': 'Files · chunks · indexing · questions — all tracked',
      'dash.card.documents': 'Documents',
      'dash.card.chunks': 'Chunks',
      'dash.card.embedded': 'Indexed',
      'dash.card.chars': 'Characters',
      'dash.card.queries': 'Questions',
      'dash.card.sessions': 'Sessions',
      'dash.bySource': 'By source (incl. feedback)',
      'dash.categoryDist': 'Category distribution',
      'dash.days14': 'Questions over the last 14 days',
      'dash.empty': 'No data yet',
      'dash.ingestionHistory': 'Ingestion history',

      'admin.headerSub': 'Admin',
      'admin.noperm': 'This account is not an admin and has no access.',
      'admin.provider.title': 'AI Provider',
      'admin.provider.sub': 'Configure the model backend used to generate answers — a cloud API (Claude / MiniMax, etc.), or a locally running inference service (Ollama / vLLM, OpenAI-compatible). Changes take effect immediately, no restart needed.',
      'admin.provider.kindAnthropic': 'Anthropic-compatible (Claude / MiniMax)',
      'admin.provider.kindOpenai': 'OpenAI-compatible (vLLM / Ollama / local service)',
      'admin.provider.baseUrlPlaceholder': 'Endpoint base_url (local example: http://localhost:11434/v1)',
      'admin.provider.modelPlaceholder': 'Model name, e.g. MiniMax-M3 / claude-sonnet-5 / qwen2.5:14b',
      'admin.provider.apiKeyPlaceholder': 'API key (usually not needed for local services)',
      'admin.provider.maxTokenPlaceholder': 'Max tokens per request',
      'admin.provider.testBtn': 'Test connection',
      'admin.provider.resetBtn': 'Reset to default',
      'admin.provider.saveBtn': 'Save',
      'admin.provider.keyHintSet': 'An API key is currently set ({{hint}}). Leaving this blank when saving keeps it unchanged.',
      'admin.provider.keyHintUnset': 'No API key set yet (usually not needed for local services).',
      'admin.provider.saving': 'Saving…',
      'admin.provider.saveFailed': 'Save failed',
      'admin.provider.saved': 'Saved — now in effect.',
      'admin.provider.testing': 'Testing…',
      'admin.provider.testFailedPrefix': 'Connection failed: ',
      'admin.provider.testSuccessPrefix': 'Connected — model replied: ',
      'admin.provider.testEmptyReply': '(empty)',
      'admin.provider.resetting': 'Resetting…',
      'admin.provider.resetFailed': 'Reset failed',
      'admin.provider.resetDone': 'Reset to the defaults from config/settings.yaml + .env.',

      'admin.users.title': 'Create User',
      'admin.users.usernamePlaceholder': 'Username',
      'admin.users.initialPasswordPlaceholder': 'Initial password (blank = generate a setup code)',
      'admin.users.adminLabel': 'Admin',
      'admin.users.createBtn': 'Create',
      'admin.users.usernameRequired': 'Username cannot be empty',
      'admin.users.createFailed': 'Create failed',
      'admin.users.createdResult': 'Created pending account {{username}}, setup code: {{setupCode}} (give it to them — shown only once)',
      'admin.users.listTitle': 'Users',
      'admin.users.colUser': 'User',
      'admin.users.colStatus': 'Status',
      'admin.users.colCreated': 'Created',
      'admin.users.tagAdmin': 'admin',
      'admin.users.tagDisabled': 'disabled',
      'admin.users.tagPending': 'pending',
      'admin.users.statusActive': 'active',
      'admin.users.deactivate': 'Disable',
      'admin.users.activate': 'Enable',
    },
  };

  var current = 'zh';
  var listeners = [];

  function onChange(fn) { listeners.push(fn); }

  function detect() {
    var lang = (navigator.language || (navigator.languages && navigator.languages[0]) || 'zh').toLowerCase();
    return lang.indexOf('zh') === 0 ? 'zh' : 'en';
  }

  function init() {
    var stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    current = (stored === 'zh' || stored === 'en') ? stored : detect();
    document.documentElement.lang = current === 'zh' ? 'zh' : 'en';
  }

  function getLang() { return current; }

  function setLang(lang) {
    current = (lang === 'zh' || lang === 'en') ? lang : current;
    try { localStorage.setItem(STORAGE_KEY, current); } catch (e) {}
    document.documentElement.lang = current === 'zh' ? 'zh' : 'en';
    applyI18n();
    listeners.forEach(function (fn) { fn(); });
  }

  function t(key, vars) {
    var table = DICT[current] || DICT.zh;
    var s = Object.prototype.hasOwnProperty.call(table, key) ? table[key] : DICT.zh[key];
    if (s === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        s = s.replace(new RegExp('\\{\\{' + k + '\\}\\}', 'g'), vars[k]);
      });
    }
    return s;
  }

  function applyI18n(root) {
    root = root || document;
    root.querySelectorAll('[data-i18n]').forEach(function (el) { el.textContent = t(el.dataset.i18n); });
    root.querySelectorAll('[data-i18n-html]').forEach(function (el) { el.innerHTML = t(el.dataset.i18nHtml); });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) { el.placeholder = t(el.dataset.i18nPlaceholder); });
    root.querySelectorAll('[data-i18n-title]').forEach(function (el) { el.title = t(el.dataset.i18nTitle); });
    var titleKey = document.documentElement.dataset.i18nDocTitle;
    if (titleKey) document.title = t(titleKey);
  }

  init();

  return { t: t, applyI18n: applyI18n, setLang: setLang, getLang: getLang, init: init, onChange: onChange };
})();
