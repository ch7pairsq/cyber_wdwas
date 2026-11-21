# reverse.protobufc

## 触发
- 二进制含 protobuf-c 特征：protobuf_c_* 符号、parse_member 循环、ProtobufC 描述符。

## 最小动作
1. tool=file_info 确认 ELF；tool=symbols 列 protobuf_c_* 与 *_get_packed_size 特征。
2. 只对三个经典 bug 面看反汇编（tool=disassemble），不通读 parse 循环：
   - required 缺失：error 路径未释放 → UAF（查 goto error 前 malloc/free 配对）。
   - bytes/length 字段：len 未校验剩余缓冲 → 堆溢出（查 memcpy 长度来源）。
   - 嵌套 message 递归：无深度限制 → 栈溢出（查递归调用链）。
3. tool=python_exec 按 ProtobufCFieldDescriptor 手工序列化触发输入。

## 命中判据
- 三面之一命中并给出触发输入构造，或读出唯一 flag{...}。
- 负判据：三面都有防护；写 ruled_out。

## 何时换路
- 泛 ELF 转 reverse.elf.static；利用转 pwn.heap.uaf。

## 踩坑
- 字段布局在 descriptor 里：先读描述符再构造输入，别按 wire format 猜。
- 按 required→bytes→递归序查，命中即停，别全量审计。

## 来源/边界
- 内部编写；不涵盖 protobuf 全量协议逆向。
