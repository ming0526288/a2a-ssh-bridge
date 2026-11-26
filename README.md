# A2A SSH Bridge

在两台 Linux 主机之间提交联调任务、查询进度。两端各运行一个用户级 systemd 服务，A 通过一条 SSH 隧道建立两个方向的端口转发。隧道断开时，systemd 自动重连。

两端只监听本机地址。A2A 接口使用 Bearer 鉴权，任务结果在内存中最多保留 24 条，每条最多保留 10 分钟。启动服务需要系统开启对应用户的 linger。两端依赖 Python 3.9+、systemd 和 OpenSSH。Codex 分析任务使用接收端已经登录的 Codex CLI。

## 安装

把仓库克隆到 A 的家目录下。首次连接新主机时，在 A 机用 SSH 验证 B 的主机指纹并加入 `known_hosts`。以下命令从实际克隆目录的上一级执行。

```bash
cd a2a-ssh-bridge &&
python3 bridge.py prepare &&
nano connection.ini
```

在配置中填写对端信息并保存后，在同一目录执行：

```bash
python3 bridge.py pair
```

在 `connection.ini` 写 B 的 SSH 地址、用户和密码。文件权限应为 600。连接信息和联调令牌均在 Git 忽略列表内。配对会在 B 的家目录下使用相同相对路径写入服务文件和联调令牌，然后启用两个端点和 A 的重连服务。两台机器均应是你信任和管理的主机。

若安装提示 linger 未开启，在对应机器用管理员权限执行 `sudo loginctl enable-linger "$USER"`，然后从 A 重新执行 `python3 bridge.py pair`。

## 两边发任务

在 A 或 B 进入各自的桥接目录。

```bash
python3 bridge.py send identity check-01 --wait
python3 bridge.py send ask check-02 --text "查看当前工作目录，概述仓库用途"
python3 bridge.py status <任务ID>
```

`identity` 给出接收端的机器名和工作区版本。`ask` 在接收端启动只读 Codex 任务。默认工作区为桥接目录的上一级。要切换项目，在接收端写 `agent.ini`：

```ini
[agent]
workspace = ../my-project
# proxy = http://127.0.0.1:8080
```

相对路径以 `bridge.py` 所在目录为起点，也可以填写绝对路径或以 `~` 开头的路径。

需要代理访问模型服务的机器可以启用 `proxy`。在没有模型网络时仍可发起 `identity`。

省略 `--wait` 时，发送命令会尽快返回任务 ID。两边可使用相同的 `run_id` 关联同轮结果。

## 选择对端 Codex 对话

列出对端账号下保存的对话。结果包含会话 ID、已设置的标题、工作目录和更新时间。若有下一页，使用结果中的 `next_cursor` 执行 `send threads list-02 --cursor "$CURSOR" --wait`。

```bash
python3 bridge.py send threads list-01 --wait
```

把选中的会话 ID 赋给本机终端的 `THREAD_ID`。任务使用这条对话原来的工作目录和上下文。也可以新建一条保存的对话，从结果中取得新的 `thread_id`。

```bash
python3 bridge.py send ask check-03 --thread-id "$THREAD_ID" --text "继续核对刚才的情况" --wait
python3 bridge.py send ask check-04 --session new --text "开始一次新的检查" --wait
```

对端可用 `codex resume "$THREAD_ID"` 打开保存的对话。默认的 `ask` 是独立临时任务。发任务和查进度仍用相同的桥接入口。

更换 B 时，先在原 B 执行 `systemctl --user disable --now a2a-bridge.service`，再在 A 改连接配置并重新配对。服务使用 A2A 0.3 的 Agent Card、`message/send` 和 `tasks/get` 接口。支持联调所需的请求和进度轮询。
