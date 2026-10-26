import json
from pathlib import Path
import jmcomic
# import aiofiles

option_file = Path(__file__).parent / 'option.yml'
option = jmcomic.create_option_by_file(str(option_file))
data = Path(__file__).parent / 'data'
data.mkdir(exist_ok=True)

history_file = data / 'history.json'
history = {}

if history_file.exists():
    with open(history_file, 'r', encoding='utf-8') as f:
        history = json.load(f)

async def write():
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f,ensure_ascii=False, indent=2)

async def get_jmcomic_pdf(id: str):
    pdf_filepath_list = history.get(id, [])
    if pdf_filepath_list:
        return pdf_filepath_list

    res = jmcomic.download_album(id, option=option)
    pdf_filepath_list = res.manifest.get_export_filepath_list('pdf')
    history[id] = pdf_filepath_list
    await write()
    return pdf_filepath_list
