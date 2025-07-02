#!/usr/bin/env python3
"""
RustDesk Server Monitor
监控 RustDesk 服务器连接信息的 Web 界面
运行方式: python rustdesk_monitor.py
nuitka --onefile --standalone --output-dir=dist --include-package=debian --include-module=importlib.metadata rustdesk_server.py
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import logging
import html
import urllib.parse

# 配置参数
OFFLINE_TIMEOUT_SECONDS = 90  # 设备离线判断时间（秒）

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="RustDesk Server Monitor", version="1.0.0")

# 数据库路径
DB_PATH = "/var/lib/rustdesk-server/db_v2.sqlite3"
MONITOR_DB_PATH = "./rustdesk_monitor.db"

# 存储在线设备信息（基于心跳包）
online_devices = {}  # {id: {last_heartbeat: datetime, info: dict}}

# 已知接口列表
KNOWN_ENDPOINTS = {"/api/heartbeat", "/api/sysinfo", "/", "/api/devices", "/health", "/favicon.ico", "/api/device/note"}

class RustDeskMonitor:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        
    def get_all_peer_ids(self) -> List[Dict[str, Any]]:
        """从原始数据库获取所有设备ID，按created_at排序"""
        try:
            if not Path(self.db_path).exists():
                logger.warning(f"数据库文件不存在: {self.db_path}")
                return []
                
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 查询所有记录，按创建时间排序
            cursor.execute("""
                SELECT id, created_at, info
                FROM peer 
                ORDER BY created_at ASC
            """)
            
            peers = []
            for row in cursor.fetchall():
                # 安全处理数据
                device_id = str(row['id']) if row['id'] else "未知"
                
                # 解析 info 字段中的 JSON 数据
                info_data = {}
                if row['info']:
                    try:
                        info_data = json.loads(row['info'])
                    except (json.JSONDecodeError, TypeError):
                        info_data = {"raw": str(row['info'])}
                
                # 格式化时间
                created_time = "未知"
                if row['created_at']:
                    try:
                        if isinstance(row['created_at'], (int, float)):
                            created_time = datetime.fromtimestamp(row['created_at']).strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            created_time = str(row['created_at'])
                    except (ValueError, TypeError, OSError):
                        created_time = str(row['created_at'])
                
                peers.append({
                    'id': device_id,
                    'created_at': created_time,
                    'ip': str(info_data.get('ip', '未知')) if info_data.get('ip') else '未知'
                })
            
            conn.close()
            return peers
            
        except Exception as e:
            logger.error(f"数据库查询错误: {e}")
            return []

class DatabaseManager:
    def __init__(self, db_path: str = MONITOR_DB_PATH):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """初始化监控数据库"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建设备信息表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS device_info (
                    id TEXT PRIMARY KEY,
                    hostname TEXT,
                    username TEXT,
                    os TEXT,
                    cpu TEXT,
                    memory TEXT,
                    version TEXT,
                    uuid TEXT,
                    created_at DATETIME,
                    last_seen DATETIME,
                    last_ip TEXT
                )
            """)
            
            # 创建心跳记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS heartbeat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT,
                    ip_address TEXT,
                    timestamp DATETIME,
                    version INTEGER,
                    FOREIGN KEY (device_id) REFERENCES device_info (id)
                )
            """)
            
            # 创建设备备注表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS device_notes (
                    device_id TEXT PRIMARY KEY,
                    note TEXT,
                    updated_at DATETIME
                )
            """)
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"初始化数据库失败: {e}")
    
    def update_device_info(self, device_info: dict, ip_address: str):
        """更新设备信息"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            device_id = str(device_info.get('id', '')) if device_info.get('id') else None
            if not device_id:
                conn.close()
                return
            
            now = datetime.now()
            
            # 安全处理字符串数据
            hostname = str(device_info.get('hostname', ''))[:255]
            username = str(device_info.get('username', ''))[:255]
            os_info = str(device_info.get('os', ''))[:500]
            cpu_info = str(device_info.get('cpu', ''))[:500]
            memory_info = str(device_info.get('memory', ''))[:100]
            version = str(device_info.get('version', ''))[:50]
            uuid = str(device_info.get('uuid', ''))[:255]
            ip_address = str(ip_address)[:45]  # IPv6最长39个字符
            
            # 检查设备是否已存在
            cursor.execute("SELECT created_at FROM device_info WHERE id = ?", (device_id,))
            result = cursor.fetchone()
            
            if result:
                # 更新现有设备
                cursor.execute("""
                    UPDATE device_info SET 
                    hostname = ?, username = ?, os = ?, cpu = ?, memory = ?, 
                    version = ?, uuid = ?, last_seen = ?, last_ip = ?
                    WHERE id = ?
                """, (hostname, username, os_info, cpu_info, memory_info,
                     version, uuid, now, ip_address, device_id))
            else:
                # 插入新设备
                cursor.execute("""
                    INSERT INTO device_info 
                    (id, hostname, username, os, cpu, memory, version, uuid, 
                     created_at, last_seen, last_ip)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (device_id, hostname, username, os_info, cpu_info, memory_info,
                     version, uuid, now, now, ip_address))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"更新设备信息失败: {e}")
    
    def log_heartbeat(self, device_id: str, ip_address: str, version: int = None):
        """记录心跳日志"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            device_id = str(device_id)[:255]
            ip_address = str(ip_address)[:45]
            version = int(version) if version and str(version).isdigit() else None
            
            cursor.execute("""
                INSERT INTO heartbeat_logs (device_id, ip_address, timestamp, version)
                VALUES (?, ?, ?, ?)
            """, (device_id, ip_address, datetime.now(), version))
            
            # 保留最近1000条心跳记录
            cursor.execute("""
                DELETE FROM heartbeat_logs 
                WHERE id NOT IN (
                    SELECT id FROM heartbeat_logs 
                    ORDER BY timestamp DESC LIMIT 1000
                )
            """)
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"记录心跳日志失败: {e}")
    
    def get_device_info(self, device_id: str) -> Dict[str, Any]:
        """获取特定设备的详细信息"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM device_info WHERE id = ?", (str(device_id),))
            result = cursor.fetchone()
            
            conn.close()
            
            if result:
                return dict(result)
            return {}
        except Exception as e:
            logger.error(f"获取设备信息失败: {e}")
            return {}
    
    def get_device_note(self, device_id: str) -> str:
        """获取设备备注"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT note FROM device_notes WHERE device_id = ?", (str(device_id),))
            result = cursor.fetchone()
            
            conn.close()
            
            return result[0] if result else ""
        except Exception as e:
            logger.error(f"获取设备备注失败: {e}")
            return ""
    
    def update_device_note(self, device_id: str, note: str):
        """更新设备备注"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            device_id = str(device_id)[:255]
            note = str(note)[:500]  # 限制备注长度
            
            # 使用 INSERT OR REPLACE 来处理新增或更新
            cursor.execute("""
                INSERT OR REPLACE INTO device_notes (device_id, note, updated_at)
                VALUES (?, ?, ?)
            """, (device_id, note, datetime.now()))
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"更新设备备注失败: {e}")

# 初始化数据库管理器和监控器
db_manager = DatabaseManager()
rustdesk_monitor = RustDeskMonitor()

def get_client_ip(request: Request) -> str:
    """安全地获取客户端IP地址"""
    try:
        # 优先从代理头获取真实IP
        if "x-forwarded-for" in request.headers:
            forwarded_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
            return forwarded_ip[:45]  # 限制长度
        elif "x-real-ip" in request.headers:
            real_ip = request.headers["x-real-ip"].strip()
            return real_ip[:45]
        elif hasattr(request, 'client') and request.client and hasattr(request.client, 'host'):
            return str(request.client.host)[:45]
        else:
            return "未知"
    except Exception as e:
        logger.error(f"获取客户端IP时出错: {e}")
        return "未知"

def safe_html_escape(text: str) -> str:
    """安全的HTML转义"""
    if not text:
        return ""
    return html.escape(str(text))

def simplify_os_info(os_string: str) -> str:
    """简化操作系统信息显示"""
    if not os_string:
        return ""
    
    os_string = str(os_string)
    # 移除 "windows / " 前缀
    if os_string.lower().startswith("windows / "):
        return os_string[10:]
    
    return os_string

@app.get("/favicon.ico")
async def favicon():
    """网站图标"""
    svg_content = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
    <!-- 背景 -->
    <rect width="32" height="32" fill="#1e40af" rx="4"/>
    
    <!-- 显示器外框 -->
    <rect x="6" y="8" width="20" height="14" fill="#374151" rx="2"/>
    <rect x="7" y="9" width="18" height="10" fill="#1f2937" rx="1"/>
    
    <!-- 显示器屏幕 -->
    <rect x="8" y="10" width="16" height="8" fill="#059669" rx="1"/>
    
    <!-- 屏幕内容 - 远程桌面图标 -->
    <rect x="10" y="12" width="3" height="1" fill="#ffffff" opacity="0.8"/>
    <rect x="10" y="14" width="5" height="1" fill="#ffffff" opacity="0.6"/>
    <rect x="18" y="12" width="4" height="4" fill="#ffffff" opacity="0.3" rx="0.5"/>
    
    <!-- 显示器底座 -->
    <rect x="14" y="22" width="4" height="2" fill="#374151" rx="1"/>
    <rect x="12" y="24" width="8" height="1" fill="#374151" rx="0.5"/>
    
    <!-- 连接指示器 -->
    <circle cx="26" cy="10" r="2" fill="#10b981"/>
    <circle cx="26" cy="10" r="1" fill="#ffffff"/>
</svg>'''
    
    return HTMLResponse(content=svg_content, media_type="image/svg+xml")

@app.post("/api/heartbeat")
async def heartbeat(request: Request):
    """接收心跳包并更新设备在线状态"""
    try:
        # 安全地获取客户端IP
        client_ip = get_client_ip(request)
        
        # 限制请求体大小
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 10240:  # 10KB限制
            raise HTTPException(status_code=413, detail="请求体过大")
        
        # 获取请求体
        body = await request.json()
        
        # 验证必要字段
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="无效的请求格式")
        
        device_id = body.get('id')
        version = body.get('ver', 0)
        
        if device_id:
            device_id = str(device_id)[:255]  # 限制长度
            
            # 记录心跳日志到数据库
            db_manager.log_heartbeat(device_id, client_ip, version)
            
            # 更新设备在线状态
            now = datetime.now()
            
            # 如果是新设备上线
            if device_id not in online_devices:
                logger.info(f"🟢 设备上线: {device_id} (IP: {client_ip})")
            
            online_devices[device_id] = {
                'last_heartbeat': now,
                'ip': client_ip,
                'info': body
            }
        
        # 返回成功响应
        return {
            "status": "ok",
            "message": "心跳包接收成功",
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理心跳包时出错: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "内部服务器错误",
                "timestamp": datetime.now().isoformat()
            }
        )

@app.post("/api/sysinfo")
async def sysinfo(request: Request):
    """接收系统信息并存储到数据库"""
    try:
        # 安全地获取客户端IP
        client_ip = get_client_ip(request)
        
        # 限制请求体大小
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 51200:  # 50KB限制
            raise HTTPException(status_code=413, detail="请求体过大")
        
        # 获取请求体
        body = await request.json()
        
        # 验证必要字段
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="无效的请求格式")
        
        # 存储设备信息到数据库
        db_manager.update_device_info(body, client_ip)
        
        # 返回成功响应
        return {
            "status": "ok",
            "message": "系统信息接收成功",
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理系统信息时出错: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": "内部服务器错误",
                "timestamp": datetime.now().isoformat()
            }
        )

@app.middleware("http")
async def log_unknown_requests(request: Request, call_next):
    """记录未知接口的请求"""
    response = await call_next(request)
    
    # 如果是未知接口且返回404
    if response.status_code == 404 and request.url.path not in KNOWN_ENDPOINTS:
        try:
            client_ip = get_client_ip(request)
            
            # 获取请求头（过滤敏感信息）
            headers = {}
            for key, value in request.headers.items():
                if key.lower() not in ['authorization', 'cookie', 'x-api-key']:
                    headers[key] = str(value)[:200]  # 限制长度
            
            logger.info(f"❓ 未知接口 - {request.method} {request.url.path} - IP: {client_ip}")
            logger.info(f"   请求头: {json.dumps(headers, ensure_ascii=False)}")
            logger.info(f"   查询参数: {dict(request.query_params)}")
            
        except Exception as e:
            logger.error(f"记录未知请求时出错: {e}")
    
    return response

@app.get("/api/devices")
async def get_devices():
    """获取所有设备信息"""
    try:
        # 从原始数据库获取所有设备ID（按created_at排序）
        all_peers = rustdesk_monitor.get_all_peer_ids()
        
        # 合并详细信息
        devices = []
        for peer in all_peers:
            device_id = peer['id']
            
            # 获取详细设备信息
            device_detail = db_manager.get_device_info(device_id)
            
            # 获取设备备注
            device_note = db_manager.get_device_note(device_id)
            
            # 检查是否在线
            is_online = False
            last_heartbeat_time = "从未"
            
            if device_id in online_devices:
                last_heartbeat = online_devices[device_id]['last_heartbeat']
                if datetime.now() - last_heartbeat < timedelta(seconds=OFFLINE_TIMEOUT_SECONDS):
                    is_online = True
                last_heartbeat_time = last_heartbeat.strftime("%Y-%m-%d %H:%M:%S")
            
            # 合并数据
            device = {
                'id': device_id,
                'note': device_note,
                'created_at': peer['created_at'],
                'original_ip': peer['ip'],
                'hostname': device_detail.get('hostname', ''),
                'username': device_detail.get('username', ''),
                'os': device_detail.get('os', ''),
                'cpu': device_detail.get('cpu', ''),
                'memory': device_detail.get('memory', ''),
                'version': device_detail.get('version', ''),
                'last_ip': device_detail.get('last_ip', peer['ip']),
                'is_online': is_online,
                'last_heartbeat_time': last_heartbeat_time
            }
            
            devices.append(device)
        
        online_count = len([d for d in devices if d['is_online']])
        
        return {
            "total": len(devices),
            "online": online_count,
            "offline": len(devices) - online_count,
            "devices": devices
        }
    except Exception as e:
        logger.error(f"获取设备信息失败: {e}")
        raise HTTPException(status_code=500, detail="获取设备信息失败")

@app.post("/api/device/note")
async def update_device_note(request: Request):
    """更新设备备注"""
    try:
        body = await request.json()
        device_id = body.get('device_id')
        note = body.get('note', '')
        
        if not device_id:
            raise HTTPException(status_code=400, detail="设备ID不能为空")
        
        # 更新备注
        db_manager.update_device_note(device_id, note)
        
        return {
            "status": "ok",
            "message": "备注更新成功",
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新设备备注失败: {e}")
        raise HTTPException(status_code=500, detail="更新备注失败")

@app.get("/", response_class=HTMLResponse)
async def index():
    """主页面"""
    try:
        # 获取统计数据
        all_peers = rustdesk_monitor.get_all_peer_ids()
        total_devices = len(all_peers)
        
        # 计算在线设备数
        online_devices_count = 0
        for peer in all_peers:
            device_id = peer['id']
            if device_id in online_devices:
                last_heartbeat = online_devices[device_id]['last_heartbeat']
                if datetime.now() - last_heartbeat < timedelta(seconds=OFFLINE_TIMEOUT_SECONDS):
                    online_devices_count += 1
        
        # 创建SVG图标的data URL
        svg_icon = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
    <rect width="32" height="32" fill="#1e40af" rx="4"/>
    <rect x="6" y="8" width="20" height="14" fill="#374151" rx="2"/>
    <rect x="7" y="9" width="18" height="10" fill="#1f2937" rx="1"/>
    <rect x="8" y="10" width="16" height="8" fill="#059669" rx="1"/>
    <rect x="10" y="12" width="3" height="1" fill="#ffffff" opacity="0.8"/>
    <rect x="10" y="14" width="5" height="1" fill="#ffffff" opacity="0.6"/>
    <rect x="18" y="12" width="4" height="4" fill="#ffffff" opacity="0.3" rx="0.5"/>
    <rect x="14" y="22" width="4" height="2" fill="#374151" rx="1"/>
    <rect x="12" y="24" width="8" height="1" fill="#374151" rx="0.5"/>
    <circle cx="26" cy="10" r="2" fill="#10b981"/>
    <circle cx="26" cy="10" r="1" fill="#ffffff"/>
</svg>'''
        
        # URL编码SVG
        encoded_svg = urllib.parse.quote(svg_icon)
        favicon_data_url = f"data:image/svg+xml,{encoded_svg}"
        
        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RustDesk 服务器监控</title>
    <link rel="icon" href="{favicon_data_url}">
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #1a202c 0%, #2d3748 100%);
            color: white;
            padding: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
        }}
        
        .header h1 {{
            font-size: 2rem;
            font-weight: 700;
        }}
        
        .header-stats {{
            display: flex;
            gap: 30px;
        }}
        
        .stat-item {{
            text-align: center;
        }}
        
        .stat-number {{
            font-size: 1.8rem;
            font-weight: 700;
            color: white;
        }}
        
        .stat-label {{
            color: rgba(255,255,255,0.8);
            font-size: 0.8rem;
            margin-top: 4px;
        }}
        
        .controls {{
            padding: 15px 20px;
            background: white;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }}
        
        .search-box {{
            padding: 10px 16px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1rem;
            width: 300px;
            max-width: 100%;
        }}
        
        .refresh-btn {{
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: transform 0.2s;
        }}
        
        .refresh-btn:hover {{
            transform: translateY(-2px);
        }}
        
        .refresh-btn:disabled {{
            opacity: 0.6;
            transform: none;
            cursor: not-allowed;
        }}
        
        .table-container {{
            overflow-x: auto;
            padding: 0 20px 20px;
        }}
        
        .devices-table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
            font-size: 0.85rem;
        }}
        
        .devices-table th {{
            background: #f1f5f9;
            padding: 12px 8px;
            text-align: left;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #e2e8f0;
            font-size: 0.8rem;
        }}
        
        .devices-table td {{
            padding: 10px 8px;
            border-bottom: 1px solid #f1f5f9;
            vertical-align: top;
        }}
        
        .devices-table tbody tr:hover {{
            background: #f8fafc;
        }}
        
        .status-badge {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            cursor: pointer;
            transition: all 0.2s;
        }}
        
        .status-online {{
            background: #dcfce7;
            color: #166534;
        }}
        
        .status-offline {{
            background: #fee2e2;
            color: #991b1b;
        }}
        
        .status-badge:hover {{
            transform: scale(1.05);
        }}
        
        .id-cell {{
            max-width: 120px;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        
        .id-display {{
            font-family: 'Monaco', 'Consolas', monospace;
            font-size: 0.8rem;
            padding: 6px 10px;
            background: linear-gradient(135deg, #f1f5f9 0%, #e2e8f0 100%);
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s;
            border: 1px solid transparent;
            color: #374151;
            margin-bottom: 4px;
        }}
        
        .id-display:hover {{
            background: linear-gradient(135deg, #dbeafe 0%, #bfdbfe 100%);
            border-color: #3b82f6;
            color: #1e40af;
            transform: translateY(-1px);
        }}
        
        .note-display {{
            font-size: 0.7rem;
            color: #6b7280;
            font-style: italic;
            margin-top: 2px;
            line-height: 1.2;
        }}
        
        .note-input {{
            width: 100%;
            padding: 6px 10px;
            border: 1px solid #d1d5db;
            border-radius: 6px;
            font-size: 0.8rem;
            background: #f9fafb;
            min-width: 150px;
        }}
        
        .note-input:focus {{
            outline: none;
            border-color: #3b82f6;
            background: white;
            box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
        }}
        
        .detail-item {{
            font-size: 0.8rem;
            max-width: 120px;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        
        .empty-state {{
            text-align: center;
            padding: 60px 30px;
            color: #64748b;
        }}
        
        .empty-state h3 {{
            font-size: 1.5rem;
            margin-bottom: 10px;
            color: #374151;
        }}
        
        .loading {{
            opacity: 0.6;
        }}
        
        .copy-toast {{
            position: fixed;
            top: 20px;
            right: 20px;
            background: #10b981;
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            font-weight: 600;
            z-index: 1000;
            animation: slideIn 0.3s ease-out;
        }}
        
        .device-detail-modal {{
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
        }}
        
        .device-detail-content {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            max-width: 500px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 25px 50px rgba(0, 0, 0, 0.25);
        }}
        
        .device-detail-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 16px;
            border-bottom: 2px solid #e2e8f0;
        }}
        
        .device-detail-title {{
            font-size: 1.25rem;
            font-weight: 700;
            color: #374151;
        }}
        
        .close-btn {{
            background: #f3f4f6;
            border: none;
            border-radius: 50%;
            width: 32px;
            height: 32px;
            cursor: pointer;
            font-size: 1.2rem;
            color: #6b7280;
            transition: all 0.2s;
        }}
        
        .close-btn:hover {{
            background: #e5e7eb;
            color: #374151;
        }}
        
        .device-detail-item {{
            margin-bottom: 16px;
            display: flex;
            flex-direction: column;
        }}
        
        .device-detail-label {{
            font-weight: 600;
            color: #6b7280;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 4px;
        }}
        
        .device-detail-value {{
            font-size: 0.9rem;
            color: #374151;
            word-break: break-all;
            background: #f9fafb;
            padding: 8px 12px;
            border-radius: 6px;
            border: 1px solid #e5e7eb;
        }}
        
        @keyframes slideIn {{
            from {{
                transform: translateX(100%);
                opacity: 0;
            }}
            to {{
                transform: translateX(0);
                opacity: 1;
            }}
        }}
        
        @media (max-width: 1200px) {{
            .devices-table {{
                font-size: 0.75rem;
            }}
            
            .devices-table th,
            .devices-table td {{
                padding: 8px 4px;
            }}
            
            .header {{
                flex-direction: column;
                text-align: center;
            }}
        }}
        
        @media (max-width: 768px) {{
            body {{
                padding: 10px;
            }}
            
            .header h1 {{
                font-size: 1.5rem;
            }}
            
            .header-stats {{
                gap: 20px;
            }}
            
            .controls {{
                padding: 15px;
                flex-direction: column;
                align-items: stretch;
            }}
            
            .search-box {{
                width: 100%;
            }}
            
            .table-container {{
                padding: 0 10px 15px;
            }}
            
            .device-detail-content {{
                margin: 20px;
                width: calc(100% - 40px);
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🖥️ RustDesk 服务器监控</h1>
            <div class="header-stats">
                <div class="stat-item">
                    <div class="stat-number" id="totalDevices">{total_devices}</div>
                    <div class="stat-label">总设备数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number" id="onlineDevices">{online_devices_count}</div>
                    <div class="stat-label">在线设备</div>
                </div>
                <div class="stat-item">
                    <div class="stat-number" id="offlineDevices">{total_devices - online_devices_count}</div>
                    <div class="stat-label">离线设备</div>
                </div>
            </div>
        </div>
        
        <div class="controls">
            <input type="text" class="search-box" id="searchBox" placeholder="搜索设备信息...">
            <button class="refresh-btn" id="refreshBtn" onclick="refreshData()">
                🔄 刷新数据
            </button>
        </div>
        
        <div class="table-container">
            <table class="devices-table" id="devicesTable">
                <thead>
                    <tr>
                        <th>设备ID</th>
                        <th>状态</th>
                        <th>主机名</th>
                        <th>用户名</th>
                        <th>CPU</th>
                        <th>内存</th>
                        <th>IP地址</th>
                        <th>备注</th>
                    </tr>
                </thead>
                <tbody id="devicesTableBody">
                    <!-- 数据将通过JavaScript动态加载 -->
                </tbody>
            </table>
        </div>
    </div>
    
    <!-- 设备详情弹窗 -->
    <div class="device-detail-modal" id="deviceDetailModal">
        <div class="device-detail-content">
            <div class="device-detail-header">
                <div class="device-detail-title">设备详情</div>
                <button class="close-btn" onclick="closeDeviceDetail()">×</button>
            </div>
            <div id="deviceDetailBody">
                <!-- 详情内容将动态加载 -->
            </div>
        </div>
    </div>
    
    <script>
        let isRefreshing = false;
        let allDevices = [];
        
        // HTML转义函数
        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text || '';
            return div.innerHTML;
        }}
        
        // 简化操作系统信息显示
        function simplifyOsInfo(osString) {{
            if (!osString) return '';
            
            // 移除 "windows / " 前缀
            if (osString.toLowerCase().startsWith('windows / ')) {{
                return osString.substring(10);
            }}
            
            return osString;
        }}
        
        // 复制到剪贴板
        function copyToClipboard(text) {{
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(function() {{
                    showCopyToast('设备ID已复制到剪贴板');
                }}).catch(function() {{
                    fallbackCopyTextToClipboard(text);
                }});
            }} else {{
                fallbackCopyTextToClipboard(text);
            }}
        }}
        
        // 兼容旧浏览器的复制方法
        function fallbackCopyTextToClipboard(text) {{
            const textArea = document.createElement('textarea');
            textArea.value = text;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            textArea.style.top = '-999999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            
            try {{
                const successful = document.execCommand('copy');
                if (successful) {{
                    showCopyToast('设备ID已复制到剪贴板');
                }} else {{
                    showCopyToast('复制失败，请手动复制');
                }}
            }} catch (err) {{
                showCopyToast('复制失败，请手动复制');
            }}
            
            document.body.removeChild(textArea);
        }}
        
        // 显示复制提示
        function showCopyToast(message) {{
            const toast = document.createElement('div');
            toast.className = 'copy-toast';
            toast.textContent = message;
            document.body.appendChild(toast);
            
            setTimeout(() => {{
                if (document.body.contains(toast)) {{
                    document.body.removeChild(toast);
                }}
            }}, 3000);
        }}
        
        // 显示设备详情
        function showDeviceDetail(deviceId) {{
            const device = allDevices.find(d => d.id === deviceId);
            if (!device) return;
            
            const osInfo = simplifyOsInfo(device.os);
            
            const detailHtml = `
                <div class="device-detail-item">
                    <div class="device-detail-label">设备ID</div>
                    <div class="device-detail-value">${{escapeHtml(device.id)}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">操作系统</div>
                    <div class="device-detail-value">${{escapeHtml(osInfo || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">版本</div>
                    <div class="device-detail-value">${{escapeHtml(device.version || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">最后心跳时间</div>
                    <div class="device-detail-value">${{escapeHtml(device.last_heartbeat_time)}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">主机名</div>
                    <div class="device-detail-value">${{escapeHtml(device.hostname || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">用户名</div>
                    <div class="device-detail-value">${{escapeHtml(device.username || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">CPU信息</div>
                    <div class="device-detail-value">${{escapeHtml(device.cpu || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">内存信息</div>
                    <div class="device-detail-value">${{escapeHtml(device.memory || '未知')}}</div>
                </div>
                <div class="device-detail-item">
                    <div class="device-detail-label">IP地址</div>
                    <div class="device-detail-value">${{escapeHtml(device.last_ip || device.original_ip || '未知')}}</div>
                </div>
                ${{device.note ? `
                <div class="device-detail-item">
                    <div class="device-detail-label">备注</div>
                    <div class="device-detail-value">${{escapeHtml(device.note)}}</div>
                </div>
                ` : ''}}
            `;
            
            document.getElementById('deviceDetailBody').innerHTML = detailHtml;
            document.getElementById('deviceDetailModal').style.display = 'flex';
        }}
        
        // 关闭设备详情
        function closeDeviceDetail() {{
            document.getElementById('deviceDetailModal').style.display = 'none';
        }}
        
        // 点击弹窗外部关闭
        document.getElementById('deviceDetailModal').addEventListener('click', function(e) {{
            if (e.target === this) {{
                closeDeviceDetail();
            }}
        }});
        
        // 更新设备备注
        async function updateDeviceNote(deviceId, note) {{
            try {{
                const response = await fetch('/api/device/note', {{
                    method: 'POST',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{
                        device_id: deviceId,
                        note: note
                    }})
                }});
                
                if (!response.ok) {{
                    throw new Error('更新备注失败');
                }}
            }} catch (error) {{
                console.error('更新备注失败:', error);
            }}
        }}
        
        // 生成设备表格行
        function generateDeviceRows(devices) {{
            allDevices = devices; // 保存设备数据用于详情显示
            
            if (!devices || devices.length === 0) {{
                return '<tr><td colspan="8" class="empty-state"><h3>暂无设备记录</h3><p>等待设备发送系统信息...</p></td></tr>';
            }}
            
            return devices.map(device => {{
                const statusClass = device.is_online ? 'status-online' : 'status-offline';
                const statusText = device.is_online ? '在线' : '离线';
                const statusIcon = device.is_online ? '🟢' : '🔴';
                
                // 安全处理和截断长文本
                const hostname = device.hostname && device.hostname.length > 15 
                    ? escapeHtml(device.hostname.substring(0, 15)) + '...' 
                    : escapeHtml(device.hostname || '');
                    
                const username = device.username && device.username.length > 12 
                    ? escapeHtml(device.username.substring(0, 12)) + '...' 
                    : escapeHtml(device.username || '');
                    
                const cpuInfo = device.cpu && device.cpu.length > 20 
                    ? escapeHtml(device.cpu.substring(0, 20)) + '...' 
                    : escapeHtml(device.cpu || '');
                    
                // 优先显示最新IP，如果没有就显示原始IP
                const displayIp = escapeHtml(device.last_ip || device.original_ip || '');
                
                // 备注显示
                const noteDisplay = device.note ? `<div class="note-display">${{escapeHtml(device.note)}}</div>` : '';
                
                return `
                    <tr>
                        <td class="id-cell">
                            <div class="id-display" onclick="copyToClipboard('${{escapeHtml(device.id)}}')" title="点击复制设备ID">${{escapeHtml(device.id)}}</div>
                            ${{noteDisplay}}
                        </td>
                        <td>
                            <span class="status-badge ${{statusClass}}" onclick="showDeviceDetail('${{escapeHtml(device.id)}}')" title="点击查看详情">
                                ${{statusIcon}} ${{statusText}}
                            </span>
                        </td>
                        <td class="detail-item" title="${{escapeHtml(device.hostname || '')}}">${{hostname}}</td>
                        <td class="detail-item" title="${{escapeHtml(device.username || '')}}">${{username}}</td>
                        <td class="detail-item" title="${{escapeHtml(device.cpu || '')}}">${{cpuInfo}}</td>
                        <td class="detail-item">${{escapeHtml(device.memory || '')}}</td>
                        <td class="detail-item">${{displayIp}}</td>
                        <td>
                            <input type="text" class="note-input" value="${{escapeHtml(device.note || '')}}" 
                                   placeholder="添加备注..." 
                                   onblur="updateDeviceNote('${{escapeHtml(device.id)}}', this.value)"
                                   onkeypress="if(event.key==='Enter') this.blur()">
                        </td>
                    </tr>
                `;
            }}).join('');
        }}
        
        // 刷新数据
        async function refreshData() {{
            if (isRefreshing) return;
            
            isRefreshing = true;
            const refreshBtn = document.getElementById('refreshBtn');
            const tableBody = document.getElementById('devicesTableBody');
            
            // 禁用按钮并显示加载状态
            refreshBtn.disabled = true;
            refreshBtn.textContent = '🔄 刷新中...';
            tableBody.classList.add('loading');
            
            try {{
                const response = await fetch('/api/devices');
                if (!response.ok) {{
                    throw new Error(`HTTP ${{response.status}}: ${{response.statusText}}`);
                }}
                
                const data = await response.json();
                
                // 更新统计数据
                document.getElementById('totalDevices').textContent = data.total || 0;
                document.getElementById('onlineDevices').textContent = data.online || 0;
                document.getElementById('offlineDevices').textContent = data.offline || 0;
                
                // 更新表格数据
                tableBody.innerHTML = generateDeviceRows(data.devices);
                
            }} catch (error) {{
                console.error('刷新数据失败:', error);
                tableBody.innerHTML = '<tr><td colspan="8" class="empty-state"><h3>数据加载失败</h3><p>请检查网络连接</p></td></tr>';
            }} finally {{
                // 恢复按钮状态
                refreshBtn.disabled = false;
                refreshBtn.textContent = '🔄 刷新数据';
                tableBody.classList.remove('loading');
                isRefreshing = false;
            }}
        }}
        
        // 搜索功能
        function setupSearch() {{
            const searchBox = document.getElementById('searchBox');
            searchBox.addEventListener('input', function(e) {{
                const searchTerm = e.target.value.toLowerCase();
                const tableBody = document.getElementById('devicesTableBody');
                const rows = tableBody.getElementsByTagName('tr');
                
                for (let row of rows) {{
                    const text = row.textContent.toLowerCase();
                    if (text.includes(searchTerm)) {{
                        row.style.display = '';
                    }} else {{
                        row.style.display = 'none';
                    }}
                }}
            }});
        }}
        
        // ESC键关闭弹窗
        document.addEventListener('keydown', function(e) {{
            if (e.key === 'Escape') {{
                closeDeviceDetail();
            }}
        }});
        
        // 页面加载完成后初始化
        document.addEventListener('DOMContentLoaded', function() {{
            setupSearch();
            refreshData(); // 初始加载数据
            
            // 每30秒自动刷新数据
            setInterval(refreshData, 30000);
        }});
    </script>
</body>
</html>"""
        
        return html_content
    except Exception as e:
        logger.error(f"生成主页面失败: {e}")
        raise HTTPException(status_code=500, detail="页面生成失败")

@app.get("/health")
async def health_check():
    """健康检查"""
    try:
        db_exists = Path(DB_PATH).exists()
        monitor_db_exists = Path(MONITOR_DB_PATH).exists()
        
        return {
            "status": "healthy",
            "rustdesk_db_exists": db_exists,
            "monitor_db_exists": monitor_db_exists,
            "rustdesk_db_path": DB_PATH,
            "monitor_db_path": MONITOR_DB_PATH,
            "online_devices": len(online_devices),
            "offline_timeout": OFFLINE_TIMEOUT_SECONDS,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat()
        }

if __name__ == "__main__":
    print("🚀 启动 RustDesk 服务器监控...")
    print(f"📊 监控地址: http://localhost:21114")
    print(f"🗄️ RustDesk数据库: {DB_PATH}")
    print(f"💾 监控数据库: {MONITOR_DB_PATH}")
    print(f"⏱️  离线判断时间: {OFFLINE_TIMEOUT_SECONDS}秒")
    print("💓 心跳包监控: /api/heartbeat")
    print("📊 系统信息监控: /api/sysinfo")
    print("📝 设备备注功能: /api/device/note")
    print("❓ 未知接口监控: 自动记录")
    print("🔄 每30秒自动刷新数据")
    print("📱 支持移动端访问")
    print("\n按 Ctrl+C 停止服务")
    
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=21114,
            log_level="info"
        )
    except KeyboardInterrupt:
        print("\n👋 服务已停止")
    except Exception as e:
        print(f"❌ 启动失败: {e}")
