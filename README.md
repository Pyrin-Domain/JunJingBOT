###君景BOT###
基于NapCat接口搭建的QQ机器人
由伟大的独角兽馆馆开发

目录约定（配置 / 数据 / 前端 各一个文件夹）
----
    config/   所有配置：config.json、sender_config.json、bot_config.json、paths_config.json
    data/     运行时数据：forward_dedup.db（去重指纹库）、forward_dedup_log.jsonl、check_point/
    webui/    网页参数面板的前端（index.html / app.js / style.css）
    Minitor/  后端代码：Websockets（连接层）/ BOTMain（流程）/ BotConfig（参数）/ WebPanel（面板）

老的根目录布局仍然兼容：config.json 或 forward_dedup.db 还在仓库根目录时，程序会继续读老的。

启动
----
    python run.py

网页参数面板：http://127.0.0.1:8390/
在 config/bot_config.json 的 web_panel 段里改地址 / 端口 / 关掉；面板里能改关键词、
搬运、提示词、连接方式等参数，保存即热加载（连接方式与 agent.enable 需重启 Bot）。

连接方式（config/config.json 的 type）
----
    "type": "ws_re"   反向：Bot 起 WebSocket 服务器（ws_reverse_host / ws_reverse_port，默认 8085），
                      NapCat 用「WebSocket 客户端」连过来；同一条连接收事件 + 发 API
    "type": "ws"      正向：Bot 连 NapCat 的「WebSocket 服务器」（ws_host / ws_port）收事件，
                      发消息走 config/sender_config.json 那条连接

    {
      "type": "ws_re",
      "access_token": "NapCat520",
      "ws_reverse_host": "127.0.0.1",
      "ws_reverse_port": 8085
    }

access_token 为 NapCat 里设置的密码，其余为地址 / 端口。没写 type 时按反向 ws_re 处理。

搬运参数（config/bot_config.json 的 auto_forward）
----
    groupid_list   监听的来源群（只有这些群里的转发卡片会被搬运）
    target_groupid 搬到哪些群（会跳过消息原本所在的群）
    userid_list    监听的私聊 QQ（留空 = 不搬私聊；面板里也认 user_id_list 这种写法）
    task_userid    搬运时另外私聊转发给这些好友
    dedup          内容去重：搬过的不再搬
    auto_img_forward_from_bot  机器人发的图片也自动搬

    "auto_img_forward_from_bot": {
      "enable": true,
      "bot_list": [
        {"bot_id": 3282647559, "type": "all",     "group_id": []},
        {"bot_id": 2027241761, "type": "group",   "group_id": [973208344]},
        {"bot_id": 3181349085, "type": "private"}
      ]
    }

每个机器人一条，type 决定生效范围：

    group    只在群聊生效（group_id 指定哪些群，留空 = 所有群聊）
    private  只在私聊生效（不需要额外指定范围）
    all      群聊与私聊都生效（group_id 仍然对群聊部分起过滤作用）

老写法 bot_id_list: [QQ 号, ...] 继续兼容，等价于每个都是 type=all。
上面这些参数都能在网页面板的「搬运与去重」页里改。
