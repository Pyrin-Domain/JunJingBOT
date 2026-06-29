1、新建历史知识库来获取上下文。
还是通过json文件来储存
|-------1、历史技能//这部分让ai总结能不能学到一些方法的范式，比如怎么进行一些处理分析之类的。纯语言，纯文本的内容
|
|
|------2、保存一些重要信息，比如伟大的馆馆的话！和一些重要的群聊信息


显然，1是方法，应当是全局的。
2也分为两类，全局重要信息和group的重要信息

我们进行结构化处理可以这样
//
history/
|
|---method.json
|
|---info.json

info.json当如是:
{
    global:[{"index":context}...]
    local:{
        group_id:[{"index":context}]
    }
}

给agent提供增删改的功能来优化info.json和method.json
希望这样的比较简洁，这样就不需要建立多重索引，context存的直接是context，而不是信息文件的路径和摘要了
不过我们还是可以加一些选项的，为了后续方便
在这样的
{'index':index,
'type': 'abstract' | 'context',
"data": {
    'context':"文本信息",
    'summary':"摘要"    
    'path':'如果是abstract，就存放原文件的地址'
    }
}
而Method是一些方法论，比如处理问题的时候应该怎么怎么样
比如要分析聊天记录的关系，尤其是回复与被回复的，直接嵌入提示词即可

