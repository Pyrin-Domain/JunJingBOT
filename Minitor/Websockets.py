import asyncio
import websockets
import json
import re
from collections.abc import Callable


def gettoken(file_path): 
    with open(file_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    token = config.get("access_token")
    port = config.get("ws_port", 3001)
    if not token:
        raise ValueError("配置文件中未找到 access_token")
    return {'token': token, 'port': port}



class NapCatBotConfig:
    def __init__(self,config_file="config.json"):
        self.info = gettoken(config_file)
        self.token = self.info['token']
        self.port = self.info['port']
        self.WS_URL = f"ws://127.0.0.1:{self.info['port']}?access_token={self.info['token']}"
