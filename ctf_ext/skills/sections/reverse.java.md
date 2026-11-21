# reverse.java

## 触发
- 附件是 .class/.jar（CAFEBABE 魔数）Java 逆向题：还原算法、找密钥或对抗混淆。

## 最小动作
1. tool=file_info 确认 class 版本；jar 先 tool=archive_extract 解包，META-INF/MANIFEST.MF 看 Main-Class 入口。
2. tool=java_decompile 反编译入口与关键类；混淆变名（a/b/aaa）以调用关系与常量为锚点定位逻辑。
3. 常量池挖密钥：tool=python_exec 扫字符串常量（密钥/盐/魔数）；算法用 tool=python_exec 复刻验证。
4. 加密壳/自定义 ClassLoader：解密逻辑在壳类里，先反编译壳；native 对齐转 reverse.native.so。

## 命中判据
- 算法复刻通过并产出唯一 flag{...}。
- 负判据：核心逻辑在壳或 native；写 ruled_out 记录已反编译类。

## 何时换路
- 反序列化利用转 web.java.deser；war 依赖发掘转 web.java.supplychain；.so 转 reverse.native.so。

## 踩坑
- 反编译器对新语法（record/switch 表达式）可能失败，退回读字节码常量。
- 别逐类全量反编译：先入口+数据流，几百个类会烧轮次。

## 来源/边界
- 内部编写；不涵盖 Android APK（apktool 未注册）。
