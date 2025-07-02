# RustDesk Server Monitor 说明文档

这是一个用于监控 RustDesk 自建服务器的 Web 工具。它提供了一个清晰的 Web 界面，用于实时查看连接到您服务器的设备状态、系统信息，并支持为设备添加备注。

## 核心功能

*   **设备状态监控**: 实时显示设备的在线或离线状态，状态根据客户端心跳包动态更新。
*   **详细信息展示**: 自动收集并展示客户端的主机名、用户名、操作系统、CPU、内存、IP 地址等详细信息。
*   **设备备注**: 可以为列表中的每个设备添加和编辑备注，方便识别和管理。
*   **动态搜索与刷新**: 支持即时搜索过滤设备，页面数据每30秒自动刷新，确保信息实时性。
*   **响应式界面**: 界面设计友好，无论是桌面浏览器还是移动设备都能获得良好的访问体验。
*   **健康检查**: 提供 `/health` 接口，方便检查监控服务及其依赖的数据库是否正常。
*   **轻量化部署**: 基于 Python、FastAPI 和 SQLite，资源占用少，部署简单。

## 环境要求

*   Python 3.7+
*   必要的 Python 库: `fastapi`, `uvicorn`

## 如何配置

在运行脚本前，您需要根据实际环境修改脚本开头的几个配置参数：

1.  **`DB_PATH`**: 
    此参数需要设置为您 RustDesk 服务器 `hbbs` 生成的 `db_v2.sqlite3` 数据库文件的 **绝对路径**。
    ```python
    # 例如:
    DB_PATH = "/var/lib/rustdesk-server/db_v2.sqlite3"
    ```

2.  **`MONITOR_DB_PATH`**:
    这是监控程序自身用于存储设备详细信息和备注的数据库。默认会在当前目录下创建 `rustdesk_monitor.db`，通常无需修改。
    ```python
    MONITOR_DB_PATH = "./rustdesk_monitor.db"
    ```

3.  **`OFFLINE_TIMEOUT_SECONDS`**:
    判断设备为离线的超时时间（单位：秒）。如果服务器超过此时间未收到某设备的心跳包，该设备将被标记为离线。默认为 90 秒。
    ```python
    OFFLINE_TIMEOUT_SECONDS = 90
    ```

## 运行步骤

### 1. 安装依赖

打开终端，安装 `fastapi` 和 `uvicorn`：
```bash
pip install fastapi uvicorn
```

### 2. 修改配置

使用文本编辑器打开 `rustdesk_monitor.py` 文件，按照上一节的说明修改 `DB_PATH`。

### 3. 启动服务

在脚本所在目录下运行以下命令：
```bash
python rustdesk_monitor.py
```
服务启动后，您会看到类似以下的输出：
```
🚀 启动 RustDesk 服务器监控...
📊 监控地址: http://localhost:21114
🗄️ RustDesk数据库: /var/lib/rustdesk-server/db_v2.sqlite3
💾 监控数据库: ./rustdesk_monitor.db
...
```

### 4. 访问监控界面

打开浏览器，访问 `http://<您的服务器IP>:21114` 即可看到监控主页。

## 客户端配置（重要）

为了让本监控工具能够接收到客户端的信息，您需要在您的 RustDesk 客户端中配置 `api-server` 参数。此参数会告诉客户端将心跳和系统信息发送到您的监控服务地址。

**配置方法:**

修改您客户端的名称（例如 `rustdesk.exe`），或在快捷方式、命令行中添加 `api-server` 参数。

**格式:**
`rustdesk-host=<hbbs_ip>,key=<key>,api-server=http://<monitor_ip>:<monitor_port>`

**示例:**
假设您的 RustDesk 服务器 IP 是 `1.2.3.4`，本监控脚本运行在 IP 为 `5.6.7.8` 的服务器上。
客户端配置应如下：
`rustdesk-host=1.2.3.4,key=your_public_key,api-server=http://5.6.7.8:21114`

配置完成后，当客户端运行时，其心跳包和系统信息就会被发送到监控服务，并记录到数据库中，最终显示在 Web 界面上。

## API 接口说明

该脚本提供了几个 API 接口用于接收数据和提供服务：

*   `POST /api/heartbeat`: 接收客户端发送的心跳包，用于判断在线状态。
*   `POST /api/sysinfo`: 接收客户端发送的详细系统信息。
*   `POST /api/device/note`: 用于更新指定设备的备注信息。
*   `GET /api/devices`: 向前端提供所有设备的列表和状态数据。
*   `GET /`: 显示监控主页的 HTML 界面。
*   `GET /health`: 提供健康检查状态。

## 编译为独立可执行文件

如果您想将此脚本编译为单个可执行文件，方便在没有 Python 环境的服务器上部署，可以使用 `Nuitka`。

1.  **安装 Nuitka:**
    ```bash
    pip install nuitka
    ```

2.  **执行编译命令:**
    脚本的注释中提供了编译命令。请注意将 `rustdesk_server.py` 替换为实际的脚本文件名 `rustdesk_monitor.py`。
    ```bash
    nuitka --onefile --standalone --output-dir=dist --include-package=debian --include-module=importlib.metadata rustdesk_monitor.py
    ```
    编译成功后，会在 `dist` 目录下生成一个可执行文件。

这是基于官方1.1.14服务端写的，可以直接使用官方的客户端1.4.0，方便纯个人使用。
