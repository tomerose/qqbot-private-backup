# astrbot_plugin_aiocensor

> [!important]
> 本项目依赖kwmatcher进行本地关键词匹配。理论上这些依赖会自动安装。

Astrbot 综合内容安全+群管插件。

自动处置功能使用了[astrbot_plugin_anti_porn](https://github.com/zouyonghe/astrbot_plugin_anti_porn)的源码。

## 兼容的适配器

已测试：

- aiocqhttp
- telegram

完全兼容（功能包括自动处置）：

- aiocqhttp

部分兼容（功能不包括自动处置）：

- telegram

理论部分兼容：

- lark

## 特点

### 多样的功能

除去最基本的文本和图片审核外，还支持新成员加入后的一段时间内强制审查、群管功能、配置违规消息处置结果转发等。

### WebUI

本项目支持通过WebUI进行管理。

### 灵活的本地关键词规则

使用&表示要求多个关键词同时出现，使用~表示排除包含特定关键词。排除条件组内部可用&连接，要求组内的所有关键词必须同时存在。

假设有如下关键词规则：

`A&B~C&D&E~F&G&H&I&J`

将被解析为两个组：

包含组：

{"A","B"}
排除组：

{"C","D","E"}
{"F","G","H","I","J"}
如果文本缺少"A"或"B"中的任意一个，匹配失败。

如果文本同时包含"C"、"D"、"E"全部三个，匹配失败。

如果文本同时包含"F"、"G"、"H"、"I"、"J" 全部五个，匹配失败。

在包含组都出现的情况下，只要任一排除组全部出现就匹配失败。

### 多提供商支持

> [!note]
> 图片链接是指的是将图片的URL地址提交给API进行审核。API会根据该URL自行下载图片内容并执行审核。若图片URL下载超时（阿里云API限制为7秒，腾讯云API限制为3秒），审核就会失败。这时会将图片文件的Base64提交给API进行重试（因为Base64可以规避下载超时问题）

除去本地审核外，还支持：

#### 阿里云内容安全（文本、图片链接）

![image](https://github.com/user-attachments/assets/d2936efc-5cb8-4855-96a2-238d538d8fe4)

开通地址：<https://www.aliyun.com/product/lvwang>

关于accesskeyID与accesskeySecret：<https://help.aliyun.com/zh/ram/user-guide/create-an-accesskey-pair>

更多信息请参阅阿里云官方文档：<https://help.aliyun.com/document_detail/2525371.html>

#### 腾讯内容安全（文本、图片链接、图片base64）

> [!note]
> 暂不支持Biztype。

![image](https://github.com/user-attachments/assets/52e57412-47fd-480a-b799-d42cc17e26ac)

![image](https://github.com/user-attachments/assets/308db025-98e5-4bae-94b4-c58d8e73ac01)

开通地址：

- <https://cloud.tencent.com/product/tms>
- <https://cloud.tencent.com/product/ims>

请参阅官方文档：<https://cloud.tencent.com/document/product/1124/37119>

#### 基于OpenAI兼容的LLM审核（文本、图片链接、图片base64）

> [!note]
> 只有VL模型才支持审核图片。

请参阅提供商文档。
