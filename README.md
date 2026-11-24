# 节点罗盘 · NodeCompass

节点罗盘是一个 Windows 桌面应用，用来在同一个界面查看 Proxyseller ISP 代理、3X-UI 节点和搬瓦工 VPS。它可以显示流量、续费日期、节点在线状态以及离线 IP 归属地。

## 使用

- 在「系统设置」中按需要配置自己的服务。不要将 API Key、SSH 私钥、主机指纹文件、订阅链接或本地数据目录提交到 GitHub。
- GeoIP 仅使用 IP2Location LITE DB3 在本机查询。请从 [IP2Location 官网](https://www.ip2location.com/database/lite) 下载 DB3 BIN，然后在「系统设置 → IP 归属地」导入。仓库和 GitHub 安装包不附带数据库，不需要 IP 定位 API Key。
- 归属地指配置代理地址在数据库里的位置，不是对网站实际看到的出口做测量。GeoIP 数据缺失不会阻止其他功能。
- Windows 安装器可选创建桌面快捷方式和登录时自启动；这两项首次安装默认关闭。

## 从源码运行

在 Windows x64 上安装 Python 3.12，在项目目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -e .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m proxy_manager
```

应用默认使用 `%LOCALAPPDATA%\ProxyManager`。测试使用临时数据。开发时可通过 `--data-dir` 指定单独的数据目录。

运行 `.\packaging\build.ps1` 可构建 Windows EXE。要制作安装器，安装 Inno Setup（需简体中文语言文件），再运行 `.\packaging\build-installer.ps1 -Compiler` 并提供 `ISCC.exe` 的路径。生成的安装器不包含任何离线 IP 数据库。

NodeCompass uses the IP2Location LITE database for [IP geolocation](https://www.ip2location.com).
