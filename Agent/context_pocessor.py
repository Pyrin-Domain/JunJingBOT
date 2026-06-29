import json
import os
import sys
import threading

current_dir = os.path.dirname(os.path.abspath(__file__))


method = {'data':[
    {'index':0,'context':"Method的修改只有单条的增删改"}
]}


class rw_tools:
    def __init__(self):
        self.method = None
        self._init_thread = threading.Thread(taget=self._init_bg,deamon = True)
        self._init_thread.start()

    def _init_bg(self):
        self.method = self.read_method()


    def write_method(self,data):
        file_path = current_dir+'/history/method.json'
        self.write(file_path,data)

    def write(self,file_path,data:dict):
        print(file_path)
        try:
            with open (file_path ,mode = 'w',encoding="utf-8") as f:
                json.dump(data,f, ensure_ascii=False, indent=4)
        except FileNotFoundError:
            print("FileNotFoundError")
            dir_path = os.path.dirname(file_path)
            os.makedirs(dir_path, exist_ok=True)
            with open (file_path ,mode = 'w',encoding="utf-8") as f:
                json.dump(data,f, ensure_ascii=False, indent=4)

    def read_method(self)->dict:
        file_path = current_dir+'/history/method.json'
        return self.read(file_path)

    def read(self,file_path)->dict:
        try:
            with open (file_path ,mode = 'r',encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print("FileNotFoundError")
            self.write(file_path,{})
            return {}
        except:
            print("Error")
        return {}
    
    def apeend_method(self,context:str):
        index = len(self.method.get('data'))
        self.method['data'].append({'index':index,'context':context})
    
    def alter_methof(self,index,context):
        self.method['data'][index] = {'index':index,'context':context}
        self.write_method(self.method)
    
    def delete_method(self,index):
        self.method['data'][index] = {'index':index,'context':'[词条已弃用]'}
        self.write_method(self.method)


     

if __name__ == "__main__":
    tool = rw_tools()
    tool.write_method(method)
    print(tool.read_method())