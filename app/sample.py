"""内置示例数据：异常登录取证场景。

三套来源时钟存在偏差（演示数据中故意不预置偏差真值）：
- gateway      时钟基本准确（NTP 校准）
- app-server   真实偏差 +60s（本地时钟快 60 秒），初始假设为 0
- mobile-app   真实偏差 -30s（本地时钟慢 30 秒），不确定度较大

审计员调整偏差假设的过程：
1. 初始：login_submit(app) 被要求先于 mfa_prompt(mobile)，但按纸面时间戳顺序不可能 → C001；
2. 设 app-server offset=60、mobile-app offset=-30 后：C001 消除，但 mfa_success 与
   token_issued 区间重叠出现歧义；同时暴露 app 方“登出后才导出”的合规声明为假 → C002；
3. 将 mobile-app 不确定度由 5s 收窄到 1s（取得 MFA 服务端回执后），歧义消除。
"""

BASE = "2026-09-18T08:"

SAMPLE_PAYLOAD = {
    "case": "LOGIN-ANOMALY-2026-0918",
    "sources": [
        {
            "id": "gateway",
            "name": "API 网关（NTP 校准）",
            "clock_offset": 0,
            "drift_ppm": 0,
            "uncertainty": 0.3,
            "reliability": 0.95,
            "kind": "server",
        },
        {
            "id": "app-server",
            "name": "业务应用服务器（时钟可疑）",
            "clock_offset": 0,
            "drift_ppm": 0,
            "uncertainty": 1.0,
            "reliability": 0.7,
            "kind": "server",
        },
        {
            "id": "mobile-app",
            "name": "移动端 App（设备本地时钟）",
            "clock_offset": 0,
            "drift_ppm": 0,
            "uncertainty": 5.0,
            "reliability": 0.6,
            "kind": "client",
        },
    ],
    "events": [
        # ---- gateway（真实时间）----
        {"id": "g1", "source": "gateway", "time": f"{BASE}01:40Z",
         "type": "session.start", "actor": "user-4417",
         "summary": "网关建立 TLS 会话，分配 sid=8f3a"},
        {"id": "g2", "source": "gateway", "time": f"{BASE}02:20Z",
         "type": "auth.token_issued", "actor": "auth-svc",
         "summary": "MFA 通过后签发访问令牌 JWT-7721"},
        {"id": "g3", "source": "gateway", "time": f"{BASE}03:20Z",
         "type": "security.force_logout", "actor": "soc-analyst",
         "summary": "风控判定异地登录，强制登出并吊销令牌"},

        # ---- app-server（本地时钟快 60s）----
        {"id": "a1", "source": "app-server", "time": f"{BASE}02:50Z",
         "type": "auth.login_submit", "actor": "user-4417",
         "summary": "应用层提交账密，返回要求二次验证"},
        {"id": "a2", "source": "app-server", "time": f"{BASE}03:50Z",
         "type": "app.dashboard_open", "actor": "user-4417",
         "summary": "用户打开运营看板"},
        {"id": "a3", "source": "app-server", "time": f"{BASE}04:10Z",
         "type": "compliance.export", "actor": "svc-backup",
         "summary": "批量导出客户台账 CSV（应用日志声称发生在强制登出之后）"},

        # ---- mobile-app（本地时钟慢 30s，抖动大）----
        {"id": "m1", "source": "mobile-app", "time": f"{BASE}01:30Z",
         "type": "auth.mfa_prompt", "actor": "user-4417",
         "summary": "移动端弹出推送二次验证", "error_ms": 200},
        {"id": "m2", "source": "mobile-app", "time": f"{BASE}01:45Z",
         "type": "auth.mfa_success", "actor": "user-4417",
         "summary": "用户在手机上确认 MFA", "error_ms": 300},
    ],
    "constraints": [
        {"from": "g1", "to": "a1", "kind": "causal",
         "note": "会话建立后才能提交登录"},
        {"from": "a1", "to": "m1", "kind": "causal",
         "note": "登录提交后才会触发 MFA 推送"},
        {"from": "m1", "to": "m2", "kind": "causal",
         "note": "先弹出验证，才能确认通过"},
        {"from": "m2", "to": "g2", "kind": "causal",
         "note": "MFA 通过后网关才签发令牌"},
        {"from": "g2", "to": "a2", "kind": "causal",
         "note": "持有令牌后才能打开看板"},
        {"from": "g3", "to": "a3", "kind": "claimed",
         "note": "应用方合规声明：导出发生在强制登出之后"},
    ],
}
