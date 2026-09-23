import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import inspect
import json
import random
import re
from pathlib import Path

import jieba

from extension import Extension

try:  # 兼容 run.py 的扁平 import 和 `Minitor.NapCatTools` 包内 import
    from forward_dedup import get_miniapp_info, get_multimsg_resid
    from Paths import Paths
except ImportError:  # pragma: no cover
    from Minitor.forward_dedup import get_miniapp_info, get_multimsg_resid
    from Minitor.Paths import Paths


# ---- 从 paths_config.json 读取图片目录（适配 WSL / 跨平台） ----
def _load_imgs_dir() -> str:
    """加载图片目录配置，优先读 paths_config.json，fallback 到本地 imgs/"""
    config_path = Paths.config_file("paths_config.json")   # config/ 优先，兼容老根目录
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        imgs_path = cfg.get("imgs_dir")
        if imgs_path:
            return imgs_path  # 直接用原始字符串（WSL 路径如 /mnt/d/...）
    except Exception:
        pass
    # Fallback：项目根目录下的 imgs/
    return str(Path(__file__).resolve().parent.parent / "imgs")


_IMGS_DIR = _load_imgs_dir()
# ----


# DEBUG = True


"""
NapCat 消息文本处理层（工具模块）

本模块只保留「单条消息」级别的文本处理与收发能力：

- 文本提取 / 上下文构建：get_clean_context、generate_structed_message_to_creat_context
- 消息内容判定：is_contain_image、is_contain_video、is_reply、is_AT、check_is_*
- 文本匹配与分词：pattern_match、tokenizer
- 文本 / 图片收发：send_message、send_img、ocr
- 历史消息与断点状态：get_history_msg、get_long_history*、check_point

Bot 的整体运行流程（事件路由、自动搬运与去重、断点恢复回放、心跳保活、
AI Agent 调度、图片批量搬运任务等）已迁移到 BOTMain.py 的 BOTMain 类中。
"""


class MessageProcessor:
    """消息文本处理层：解析、判定、上下文构建与文本/图片收发。"""

    def __init__(self, napcat_api, extension=None):
        """
        :param napcat_api: NapCatAPIInterface 实例（连接由 BOTMain 统一创建并共享）
        :param extension: Extension 实例，缺省时内部自建（用于 OCR）
        """
        self.nc = napcat_api
        self.extension = extension if extension is not None else Extension()
        self.extraconfig = {"tokenizer": False, "recent": True}
        self.user_id: str = None
        self._connect_lock = asyncio.Lock()  # 防止并发重复建连
        self.check_point: dict = {}
        self._checkpoint_save_task: asyncio.Task = None

    async def ocr_and_send(self, group_id, url):
        ocr_answer = await self.extension.napcat_ocr(url)
        await self.ensure_connected()
        await self.nc.send_group_message(group_id=group_id, message=ocr_answer["text"])

    def get_url(self, msg_event) -> list[str]:
        res = []
        for item in msg_event.get("message", []):
            item_type = item.get("type", "")
            if item_type != "image":
                continue
            url = (item.get("data") or {}).get("url")
            if not url:
                continue
            res.append(url)
        return res

    def is_contain_image(self, msg_event) -> bool:
        for item in msg_event.get("message", []):
            item_type = item.get("type", "")
            if item_type == "image" and not item.get("data", {}).get("summary"):
                return True
        return False

    def is_contain_video(self, msg_event) -> bool:
        for item in msg_event.get("message", []):
            item_type = item.get("type", "")
            if item_type == "video":
                return True
        return False

    async def ocr(self, event):
        reply_id = self.is_reply(event)
        if not reply_id:
            return
        await self.ensure_connected()
        res = await self.nc.get_message(message_id=reply_id)
        msg_event = res.get("data", {})

        url_list = self.get_url(msg_event)

        for url in url_list:
            asyncio.create_task(self.ocr_and_send(group_id=event["group_id"], url=url))
        return

    async def is_AT(self, event: dict, user_id: list[str]) -> list | None:
        """检查消息是否包含@指定用户"""
        temp = []
        info = event.get("message", [])
        for item in info:
            item_type = item.get("type", "")
            if item_type != "at":
                continue
            qq_id = str((item.get("data") or {}).get("qq", ""))
            if qq_id in user_id and qq_id not in temp:
                temp.append(qq_id)
        return temp

    # async def get_message_history(
    #     self, message_id: int
    # ) -> dict["context":str, "index":int]:
    #     """获取指定消息的历史记录"""
    #     await self.ensure_connected()
    #     event = await self.nc.get_message(message_id)
    #     raw_msg = event["data"]["raw_message"]
    #     match = re.match(r"\[CQ:reply,id=(\d+)\]", raw_msg)
    #     if match:
    #         reply_msg_id = int(match.group(1))
    #         clean_msg = re.sub(r"^\[CQ:reply,id=\d+\]", "", raw_msg).strip()
    #         temp = await self.get_message_history(reply_msg_id)
    #         return {
    #             "context": temp["context"] + "\n" + str(temp["index"] + 1) + clean_msg,
    #             "index": temp["index"] + 1,
    #         }
    #     else:
    #         return {"context": "1" + raw_msg, "index": 1}

    async def get_group_latest_msg(self, group_id):
        await self.ensure_connected()
        event = await self.nc.get_group_msg_history(
            group_id=group_id, count=1, reverse_order=True
        )
        messages = event["data"]["messages"]
        if not messages:
            print(f"[警告] 群 {group_id} 暂无消息或无法访问")
            return None
        return messages[0]

    async def ensure_connected(self):
        """确保 API WebSocket 已连接且 reader 存活（带锁，自动重连）"""
        async with self._connect_lock:
            # 检查 reader 是否还活着（连接可能已异常断开）
            reader_dead = (
                self.nc._reader_task is not None and self.nc._reader_task.done()
            )
            if reader_dead:
                # 等待 reader 的 finally 清理完成
                try:
                    await self.nc._reader_task
                except Exception:
                    pass
                self.nc._reader_task = None
            if self.nc._ws_conn is None or self.nc._reader_task is None:
                await self.nc.connect()

    async def send_message(self, event, message):
        """根据事件类型发送消息"""
        if isinstance(message, list):
            n = len(message)
            index = random.randint(0, n - 1)
            message = message[index]

        if self.extraconfig["tokenizer"]:
            message = self.tokenizer(message)

        await self.ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_message(
                group_id=event["group_id"], message=message
            )
            asyncio.create_task(self.check_point_active_get(event["group_id"]))
        elif event["message_type"] == "private":
            await self.nc.send_private_message(
                user_id=event["user_id"], message=message
            )

    async def check_point_active_get(self, group_id):
        await self.ensure_connected()
        latest_msg = await self.get_group_latest_msg(group_id)
        if latest_msg:
            self.check_point[f"{group_id}"] = latest_msg

    async def send_img(self, event, img_addr, summary="君景发图"):
        """根据事件类型发送消息"""
        if isinstance(img_addr, list):
            n = len(img_addr)
            index = random.randint(0, n - 1)
            img_addr = img_addr[index]

        await self.ensure_connected()
        if event["message_type"] == "group":
            await self.nc.send_group_img(
                group_id=event["group_id"], img_addr=img_addr, summary=summary
            )
            asyncio.create_task(self.check_point_active_get(event["group_id"]))
        elif event["message_type"] == "private":
            await self.nc.send_private_img(
                user_id=event["user_id"], img_addr=img_addr, summary=summary
            )

    def get_clean_context(self, event, keepAt=False) -> str:
        DEBUG = True
        context = ""
        for item in event.get("message", []):
            msg_type = item.get("type", "")
            if msg_type == "text":
                context += (item.get("data") or {}).get("text", "")
                continue
            if msg_type == "image":
                context += "图片"
                context += (item.get("data") or {}).get("summary", "")
                continue
            if keepAt and msg_type == "at":
                qq_id = (item.get("data") or {}).get("qq", "all")
                context += f"[CQ:at,qq={qq_id}]"
        DEBUG and print(context)
        return context

        
    async def pattern_match(self, event, pattern_config: list[dict[str, any]]):
        clean_msg = self.get_clean_context(event)
        ret: bool = False
        for pattern in pattern_config:
            solution = pattern.get("solution")
            regex = pattern.get("regex")
            if not solution or not regex:
                continue
            params = pattern.get("params", {})
            filter = params.pop("filter", None)
            if filter:
                if filter.get("type") == "ban":
                    if str(event.get("group_id")) in filter.get("data"):
                        continue
            if re.search(regex, clean_msg, re.I):
                # 自动检测 solution 是否需要 event 参数
                sig = inspect.signature(solution)
                if "event" in sig.parameters:
                    asyncio.create_task(solution(event=event, **params))
                else:
                    asyncio.create_task(solution(**params))
                ret = True
                continue
                # return True
        return ret

    async def read_check_point(self, user_id):
        file_name = str(Paths.check_point_dir() / f"{user_id}.json")
        try:
            with open(file_name, mode="r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print("FileNotFoundError")
            dir_path = os.path.dirname(file_name)
            os.makedirs(dir_path, exist_ok=True)
            with open(file_name, mode="w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=4)
                return {}
        except:
            print("error")
        return {}

    async def _save_check_point(self):
        """将当前断点写入磁盘"""
        DEBUG = False
        try:
            file_name = str(Paths.check_point_dir() / f"{self.user_id}.json")
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
            with open(file_name, "w", encoding="utf-8") as f:
                json.dump(self.check_point, f, ensure_ascii=False, indent=4)
            if self.check_point:
                DEBUG and print(
                    f"[断点保存] 已写入 {len(self.check_point)} 条断点到 {file_name}"
                )
        except Exception as e:
            print(f"[断点保存] 异常: {e}")

    async def _checkpoint_save_loop(self):
        """后台每 10 秒将内存中的断点刷入磁盘"""
        while True:
            await asyncio.sleep(10)
            try:
                await self._save_check_point()
            except Exception as e:
                print(f"[断点保存] 写入失败: {e}")

    def start_checkpoint_save(self):
        """启动断点定时保存任务（需在事件循环中调用）"""
        if self._checkpoint_save_task is None or self._checkpoint_save_task.done():
            self._checkpoint_save_task = asyncio.create_task(self._checkpoint_save_loop())

    async def get_long_history_backward(self, group_id, start_id, end_id):
        DEBUG = False
        remaining = 20  # 最大搜索20次
        part = []
        DEBUG = True
        last_msg_id = start_id
        while remaining > 0:
            DEBUG and print("reamaining = ", remaining)
            await self.ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][-1]["message_seq"]
            icl_history["data"]["messages"].pop()
            part = icl_history["data"]["messages"] + part
            remaining -= 1
            for item in icl_history["data"]["messages"]:
                if item["message_seq"] == end_id:
                    return part
            if item["message_seq"] == end_id:
                return part
        DEBUG and print("NotFind")
        return part

    async def get_long_history_only_processor(
        self, group_id, start_id, end_id, processor, params
    ) -> None:
        remaining = 200  # 最大搜索200次
        DEBUG = True

        last_msg_id = start_id

        while remaining > 0:
            print("reamaining = ", remaining)
            await self.ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][-1]["message_seq"]
            last_msg = icl_history["data"]["messages"].pop()
            remaining -= 1
            for item in icl_history["data"]["messages"]:
                await processor(event=item, **params)
                if item["message_seq"] == int(end_id):
                    DEBUG and print("FIND")
                    return
            if last_msg_id == end_id:
                await processor(event=last_msg, **params)
                DEBUG and print("FIND")
                return
        return

    def check_is_miniapp(self, msg_event: dict) -> dict:
        """检测小程序卡片，返回 title/desc/link/context（另含 appid/preview/url）"""
        return get_miniapp_info(msg_event)

    def check_is_multimsg_forward(self, msg_event: dict) -> str | None:
        """
        检测是否为新版合并转发JSON卡片
        :return: resid 字符串（是转发卡片时），否则 None
        """
        return get_multimsg_resid(msg_event)

    def check_is_old_forward(self, msg_event: dict) -> bool:
        """确定是不是旧版forward"""
        for seg in msg_event.get("message", []):
            if seg["type"] == "forward":
                return True
        return False

    def is_reply(self, msg_event: dict) -> int | None:
        for seg in msg_event.get("message", []):
            if seg["type"] == "reply":
                return seg["data"]["id"]
        return

    async def get_forward_try(self, msg_event: dict) -> str:
        res = ""
        for msg in msg_event.get("message", []):
            for seg in msg.get("data", {}).get("content", []):
                res += await self.generate_structed_message_to_creat_context(seg)
        return res

    async def generate_structed_message_to_creat_context(
        self, event, isOCR=False
    ) -> str:
        ###感觉可以做一个池来达到Cache命中的效果，因为需要持续去寻找转发消息，尤其是回复比较多的情况
        ###恰好有future可以达到，但是不适合该场景，因为访存和发包的时间肯定天差地别，发包还有网络压力 命中 or get_reply
        ###但是压力不大，效果不显著
        DEBUG = False
        temp = ""
        message_id = event["message_id"]
        DEBUG and print("获取sender")
        sender = event.get("sender") or {}
        user_id = event.get("user_id") or sender.get("user_id", "未知")
        name = sender.get("card") or sender.get("nickname", "未知")
        temp += (
            f"消息发送者{name}，其QQ号为{user_id},\t发送消息[message_id:{message_id}]:"
        )
        DEBUG and print(temp)
        mini_app_info = self.check_is_miniapp(event)
        if mini_app_info:
            return f"[这是一条小程序消息{mini_app_info.get('context')}]"
        if self.check_is_old_forward(event):
            DEBUG and print("转发模块tradition Begin")

            res = await self.nc.get_forward_msg({"message_id": message_id})

            if res["status"] == "failed":
                print(f"[转发模块tradition] 获取转发消息失败: {res.get('message')}")
                return await self.get_forward_try(event)
                # return f"retcode:{res.get('retcode')}"

            messages = res["data"]["messages"]
            ret = ""
            ret_temp = ""
            for message_event in messages:
                ret_temp = await self.generate_structed_message_to_creat_context(
                    message_event
                )
                if re.search(r"retcode:1200", ret_temp):
                    # DEBUG and print("DEBUG: 消息已过期或者为内层消息，无法获取转发消息")
                    print('\n\n\n\n')
                    print(message_event)
                    print('\n\n\n\n')
                    inner_content = message_event.get("content", [])
                    if not inner_content:
                        ret += "消息已过期或者为内层消息，无法获取转发消息"
                        # DEBUG and print("DEBUG: 消息已过期或者为内层消息，无法获取转发消息")
                        continue
                    for seg in inner_content:
                        ret += await self.generate_structed_message_to_creat_context(seg)
                    continue
                else:
                    ret += ret_temp
            temp += f"这是一条转发消息,内容如下[{ret}]\n"
            DEBUG and print("转发模块tradition End")
            return temp
        rid = self.check_is_multimsg_forward(event)
        if rid:
            DEBUG and print("转发模块Json Begin")
            await self.ensure_connected()
            res = await self.nc.get_forward_msg({"message_id": rid})
            if res["status"] == "failed":
                print(f"[转发模块Json] 获取转发消息失败: {res.get('message')}")
                return ""
            messages = res["data"]["messages"]
            ret = ""
            for message_event in messages:
                if (
                    not message_event.get("message")
                    or len(message_event["message"]) == 0
                ):

                    ret += "[这可能是嵌套的聊天记录，解析失败]"
                ret += await self.generate_structed_message_to_creat_context(
                    message_event
                )
            temp += f"这是一条转发消息,内容如下[{ret}]\n"
            DEBUG and print("转发模块json End")
            return temp
        reply_id = self.is_reply(event)
        if reply_id:
            await self.ensure_connected()
            res = await self.nc.get_message(message_id=reply_id)
            if res["status"] == "failed":
                return ""
            message = res["data"]
            ret = ""
            ret += await self.generate_structed_message_to_creat_context(message)
            temp += f"这是一条回复消息,被回复的内容如下[\n\t{ret}\n\t]\n"

        for seg in event.get("message", []):
            seg_type = seg.get("type", "")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                temp += seg_data.get("text", "")
                continue
            if seg_type == "image":
                temp += "图片"
                temp += seg_data.get("summary", "[]")
                url = seg_data.get("url")
                if not url:
                    continue
                if not isOCR:
                    temp += f'\n\t{{"url":{url}}}\n'
                    continue
                ocr_res = (await self.extension.napcat_ocr(url))["text"]
                temp += f"ocr结果{ocr_res}"
                continue
            if seg_type == "at":
                temp += f"[CQ:at,qq={seg_data.get('qq')}]"
        return temp

    async def set_tokenizer(self, event):
        raw_message = event["raw_message"]
        match = re.search(r"True", raw_message)
        if match:
            self.extraconfig["tokenizer"] = True
            await self.send_message(event=event, message="set tokenizer True")
            return True
        match = re.search(r"False", raw_message)
        if match:
            await self.send_message(event=event, message="set tokenizer True")
            self.extraconfig["tokenizer"] = False
            return True
        return False

    async def get_long_history(self, group_id, message_id, remaining: int):
        if remaining <= 0:
            return []
        part = []
        DEBUG = True
        last_msg_id = message_id
        while remaining > 0:
            DEBUG and print("reamaining = ", remaining)
            await self.ensure_connected()
            icl_history = await self.nc.get_group_msg_history(
                group_id=group_id, message_seq=last_msg_id
            )
            last_msg_id = icl_history["data"]["messages"][0]["message_seq"]
            DEBUG and print(icl_history["data"]["messages"])
            DEBUG and print("last_msg_id", last_msg_id)
            icl_history["data"]["messages"].pop()
            part = icl_history["data"]["messages"] + part
            remaining -= 1

        return part

    async def get_history_msg(
        self, event=None, message_seq=None, count=6, reverse_order=False
    ) -> str:
        if not event:
            return ""
        await self.ensure_connected()
        if event.get("message_type") == "group":
            icl_history = await self.nc.get_group_msg_history(
                group_id=event["group_id"],
                reverse_order=reverse_order,
                message_seq=message_seq or event.get("message_seq", None),
                count=count,
            )
        elif event.get("message_type") == "private":
            icl_history = await self.nc.get_friend_msg_history(
                user_id=event["user_id"],
                reverse_order=reverse_order,
                message_seq=message_seq or event.get("message_seq", None),
                count=count,
            )
        else:
            return ""
        parts = []
        for ctx in icl_history["data"]["messages"]:
            parts.append(await self.generate_structed_message_to_creat_context(ctx))
        return "\n".join(parts)

    async def get_long_history_test(self, event):
        DEBUG = False
        DEBUG and print("enter History Debug")
        res = await self.get_long_history(
            group_id=event["group_id"], message_id=event["message_id"], remaining=10
        )
        DEBUG and print(res)
        DEBUG and print("end")

    @staticmethod
    def tokenizer(text: str) -> str:
        """中文分词：基于 jieba 分词库，返回空格分隔的字符串。
        示例:
            tokenizer("我是独角兽。")
            → '我 是 独角兽 。'
        """
        return " ".join(jieba.cut(text))

