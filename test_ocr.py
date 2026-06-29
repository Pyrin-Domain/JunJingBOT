"""OCR 测试：下载 QQ 图片 → PaddleOCR 识别"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from Minitor.extension import Extension

URL = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=EhSkXDOmtQsKXgdJ8ngp7ep0ktGa4Bi9qgYg_wooh--F-oaqlQMyBHByb2RQgL2jAVoQPVFycTKUo05AILWtobjRynoCKk6CAQJuag&rkey=CAESMP06IXoXB4s24PQlzZkU32iGnKRFO_sppjnvRn_cMIPsOqgdR9CXgjRP7aqGuFdpwA"

async def main():
    print("[测试] 初始化 OCR 扩展（后台加载模型）...")
    ext = Extension()
    print("[测试] 等待模型就绪并 OCR...")
    result = await ext.napcat_ocr(URL)
    print(f"[结果] 识别文本:\n{result['text']}")

if __name__ == "__main__":
    asyncio.run(main())
