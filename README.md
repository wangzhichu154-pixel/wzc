# VPS 部署指南

本仓库提供一套通用的 VPS 部署脚手架，适合把静态站点、Node/Python/Go 等 Web 服务部署到 Linux VPS。

## 服务器要求

- Ubuntu 22.04/24.04 或 Debian 12
- 一个具有 sudo 权限的用户
- 已将域名解析到 VPS 公网 IP（可选，但推荐）
- 本地机器能够通过 SSH 连接 VPS

## 一键初始化 VPS

在本地执行：

```bash
./scripts/deploy_vps.sh --host root@your-vps-ip --domain example.com --app-name wzc
```

脚本会在 VPS 上完成：

1. 安装基础依赖（Git、curl、ufw、nginx、certbot）。
2. 配置防火墙，仅开放 SSH、HTTP、HTTPS。
3. 创建应用目录 `/opt/wzc`。
4. 安装 nginx 反向代理配置。
5. 可选：如果提供域名，则申请 Let's Encrypt HTTPS 证书。

## 部署应用代码

初始化完成后，可以将应用构建产物或源码同步到 VPS：

```bash
rsync -az --delete ./ root@your-vps-ip:/opt/wzc/current/
```

如果你的应用是长期运行的服务，建议复制并启用 systemd 模板：

```bash
sudo cp /opt/wzc/current/deploy/systemd/wzc.service /etc/systemd/system/wzc.service
sudo systemctl daemon-reload
sudo systemctl enable --now wzc
```

## nginx 配置

nginx 模板位于 `deploy/nginx/wzc.conf`。默认将外部请求反向代理到本机 `127.0.0.1:3000`。

如果应用监听端口不同，请修改：

```nginx
proxy_pass http://127.0.0.1:3000;
```

## systemd 配置

systemd 模板位于 `deploy/systemd/wzc.service`。默认执行：

```bash
/usr/bin/env bash -lc 'npm install --omit=dev && npm start'
```

如果项目不是 Node.js，请根据你的启动命令修改 `ExecStart`。

## 常用运维命令

```bash
sudo systemctl status wzc
sudo journalctl -u wzc -f
sudo systemctl restart wzc
sudo nginx -t
sudo systemctl reload nginx
```
