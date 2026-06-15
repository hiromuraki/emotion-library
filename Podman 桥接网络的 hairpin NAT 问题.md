# Podman 桥接网络的 hairpin NAT 问题

## 1. 环境

### 1.1 硬件与网络

```
路由器 (DNS: *.daveny.top → 192.168.31.3)
  ├── 迷你主机 / Homelab
  │     系统: Rocky Linux 9.5
  │     IP: 192.168.31.3
  │     Podman: 5.6.0
  │     防火墙: none (无 firewalld, nft list ruleset 为空)
  │     rp_filter: all=0, default=1
  │
  └── 开发机
        IP: 192.168.31.129
```

### 1.2 部署的服务

| Pod | 网络模式 | 端口 | 用途 |
|---|---|---|---|
| Caddy | host | `:443`, `:80` | TLS 反代 |
| SeaweedFS | bridge (默认) | S3 网关 `hostIP:127.0.0.1, hostPort:8333` | S3 兼容存储 |
| emotion-library | bridge (默认) | `hostIP:127.0.0.1, hostPort:3050 → :8000` | 表情库应用 |

### 1.3 Caddy 转发规则

```
s3.daveny.top              → reverse_proxy 127.0.0.1:8333
emotion-library.daveny.top → reverse_proxy 127.0.0.1:3050
```

### 1.4 访问 S3 的四条路径

| 来源 | 路径 | 结果 |
|---|---|---|
| 外部 (192.168.31.129) | `s3.daveny.top:443` → Caddy → `127.0.0.1:8333` | ✅ |
| 宿主机 | `curl https://s3.daveny.top` → loopback → Caddy → `127.0.0.1:8333` | ✅ |
| 开发机上的 emotion-library | `s3.daveny.top:443` → Caddy → `127.0.0.1:8333` | ✅ |
| **Homelab 上的 emotion-library 容器** | `s3.daveny.top:443` → ??? | ❌ |

---

## 2. 问题

**现象**：emotion-library 容器内 HTTP 502，错误详情为 `ConnectionRefusedError: [Errno 111] Connection refused`，连接目标 `192.168.31.3:443`。

**容器内诊断**：
```text
$ curl -v https://s3.daveny.top
* Host s3.daveny.top:443 was resolved.
* IPv4: 192.168.31.3
* Trying 192.168.31.3:443...
* connect to 192.168.31.3 port 443 from 10.89.0.13 port 47548 failed: Connection refused
```

- DNS 解析正常 ✅
- 宿主机 curl 正常 ✅
- 外部设备访问正常 ✅
- 容器内连宿主机 443 → Connection refused ❌

---

## 3. 尝试过的方案

### 3.1 Bridge + HTTPS（默认配置，失败）

```
网络: bridge (默认)
S3_ENDPOINT_URL: https://s3.daveny.top
```

- DNS 解析正确（`s3.daveny.top` → `192.168.31.3`）
- TCP SYN 到达宿主机但回包路径异常
- **结论**：Podman bridge CNI 没有内置 hairpin NAT 支持。容器通过宿主机外部 IP 访问宿主机端口时，回程包无法正确 SNAT/DNAT 回到 bridge 内的源容器

### 3.2 Pasta + HTTPS（失败）

```
网络: pasta
S3_ENDPOINT_URL: https://s3.daveny.top
```

- 源 IP 变成了宿主机自己（`192.168.31.3` → `192.168.31.3`）
- 仍然 `Connection refused`
- 已排除 rp_filter（`all = 0`）
- Caddy 确认监听 `*:443`
- 无 firewalld，nftables 规则表为空
- 尝试 `--network pasta:-T,443` 显式转发 443 端口，依然不通
- **结论**：pasta 在向宿主机自身 IP 发起 TCP 连接时，socket 创建/投递方式导致连接失败。根因不明，可能与 pasta 5.x + Rocky 9.5 内核的 local delivery 路径有关，不值得继续深挖

### 3.3 Pasta + 显式端口转发 `-T,443`（失败）

- 在 3.2 基础上添加端口转发参数
- 结果不变，Connection refused

### 3.4 Bridge + HTTP 内部直连（✅ 有效）

```
网络: bridge (默认)
S3_ENDPOINT_URL: http://host.containers.internal:8333
```

- `host.containers.internal` 解析到 bridge 网关（如 `10.89.0.1`）
- 容器通过网关直连宿主机的 S3 端口 8333
- HTTP 明文，不经 Caddy、不经 TLS、不触发 hairpin NAT
- **结论**：有效。外部访问仍走 Caddy → `127.0.0.1:8333`，不受影响

### 3.5 Host 网络（理论上可行，未测试）

```
网络: host
S3_ENDPOINT_URL: https://s3.daveny.top
```

- 容器直接共享宿主机网络栈
- 无 hairpin NAT 问题、无端口映射复杂度
- 代价：失去网络隔离，容器可直接访问宿主机所有端口和网络接口
- 端口绑定需额外管理（容器直接占宿主机端口）

---

## 4. Bridge vs Pasta vs Host

| | Bridge (默认) | Pasta | Host |
|---|---|---|---|
| 网络命名空间 | 独立 | 独立 | 共享宿主机 |
| 容器内 `localhost` | 容器自己 | 容器自己 | 宿主机 |
| 访问宿主机 IP | ❌ hairpin NAT 问题 | ⚠️ 本次场景不通 | ✅ 直接 |
| 端口映射 | 需要 hostPort | 可选 | 不需要（直接占） |
| 隔离性 | 强 | 中 | 无 |
| 性能 | 有 NAT 开销 | 用户态转发 | 零开销 |

---

## 5. 诊断方法论

容器网络故障通用排查顺序：

1. **DNS** — `podman exec <container> nslookup <host>`
2. **连通性** — `podman exec <container> curl -v <url>`
3. **对比宿主机** — 宿主机上执行相同 `curl`，区分「服务故障」还是「网络故障」
4. **网络模式** — `podman inspect <container> --format '{{.HostConfig.NetworkMode}}'`
5. **防火墙** — `nft list ruleset` 或 `iptables -L -n -v`
6. **rp_filter** — `sysctl net.ipv4.conf.all.rp_filter`
7. **服务监听地址** — `ss -tlnp | grep <port>`

关键判断法则：**宿主机通 + 容器不通 = 网络层问题，不是服务问题**。
