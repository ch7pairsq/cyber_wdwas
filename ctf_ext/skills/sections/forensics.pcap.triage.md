# forensics.pcap.triage

## 触发
- 附件是 pcap/pcapng；先确认链路类型、包数、时间范围，再选协议深入。

## 最小动作
1. tool=file_info path=...；tool=strings path=... min_len=4 找协议/域名/凭据线索。
2. tool=python_exec
   code: 用标准库 struct 解析 pcap 全局头和每包记录；按 TCP/UDP 端口提取 payload，打印可打印片段。pcapng 先解 EPB。
3. 只对可疑流做重组；USB HID 按报告描述符和输入报告逐包解析，不要猜键位。

## 命中判据
- 重组/解码输出逐字含唯一 flag{...}，或还原凭据/文件。
- 负判据：只有加密流量且无密钥；停卡记录已提取的流。

## 何时换路
- 抓到加密 blob 且有密钥/参数：转 crypto 卡。
- 流量里是编码文本：转 crypto.encoding；是文件传输：提取后转 forensics.archive_layers。

## 踩坑
- 注意字节序与 snaplen；不假设有抓包工具。
- payload 按五元组分流，别拼大字符串。
- 附件同有程序和流量：程序出算法、流量出密文，先逆算法再解流量。

## 来源/边界
- 来源：ljagiello/ctf-skills@20d182d ctf-forensics/SKILL.md（MIT），人工蒸馏。
