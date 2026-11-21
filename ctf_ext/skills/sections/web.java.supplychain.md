# web.java.supplychain

## 触发
- 附件或路径暴露 jar/war；Java 栈 web 应用，需从包内发掘可利用点。

## 最小动作
1. tool=file_info 识真身；tool=archive_extract 解包到子目录，读 WEB-INF/web.xml 找 servlet/filter/mapping 入口。
2. 盘点 WEB-INF/lib/：文件名即 GAV 坐标，tool=python_exec 列版本清单，对照已知漏洞版本段（反序列化库、旧组件、默认凭据）。
3. tool=java_decompile 反编译业务 class 找注入点与硬编码凭据；properties/yml 查连接串与密钥。
4. 命中可利用点转对应卡；依赖清单与入口映射登记 evidence。

## 命中判据
- 定位可利用点并给出证据（依赖版本+触发路径），或读出唯一 flag{...}。
- 负判据：依赖全为新版且业务代码无注入；写 ruled_out。

## 何时换路
- 反序列化特征转 web.java.deser；上传点转 web.upload。

## 踩坑
- spring boot fat jar 依赖在 BOOT-INF：先解外层再进内层。
- lib 下几百个 jar 不逐个反编译：先按文件名对漏洞库清单，只深挖命中项。

## 来源/边界
- 内部编写；不涵盖源码泄露与 git 历史（见 forensics.git.history）。
