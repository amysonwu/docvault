# 支持与沟通

这个项目由我一个人维护，时间有限。为了两边都省事，把渠道和预期写清楚。

## 去哪说

| 事项 | 渠道 |
|---|---|
| 用了报错、疑似 Bug | [Issues](https://github.com/amysonwu/docvault/issues) 选 bug report 模板 |
| 想要新功能 | Issues 选 feature request 模板，写清业务场景，别只写「希望支持 xxx」 |
| 安全漏洞 | 仓库 Security 页面 → Report a vulnerability。**不要开公开 Issue** |
| 商业合作、定制开发 | Issues 开一个标题带 `[商业]` 的议题，见 [COMMERCIAL.md](COMMERCIAL.md) |
| 想直接改代码 | 先开 Issue 对设计，再提 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md) |

## 响应预期

- 正常情况下我尽量在 3 个工作日内回一次，但**不承诺 SLA**，也不保证有问必答
- 超过一周没动静，可以在原议题下补一句「还在吗」，比新开一个更有效
- 安全漏洞优先级最高，会先确认再定修复时间

## 提问请带上这些

有这几样，多数问题我一次就能定位：

1. 版本：页面左下角的 build 号，或 `curl http://<host>/api/health` 返回的 `build`
2. 部署方式：`docker compose` 还是别的，OnlyOffice 是否启用
3. 复现步骤：从哪个页面点什么，做到哪一步出错
4. 日志：`docker compose logs app --tail 100` 的输出
5. 截图或录屏，有的话最好

## 不提供什么

说在前面，免得互相等：

- 不加微信、QQ、电话，也不做一对一私信答疑。所有技术问题放在 Issue 里，别人也能搜到
- 不远程登录你的服务器排障
- 不代部署、不代运维
- 不接受「先做，做完了看效果给钱」
- 提了需求不等于排进路线图，排期看 [ROADMAP](docs/ROADMAP.md) 和实际使用人数

想让我优先做某件事，两条路：把场景写清楚（真实使用场景比「很重要」有说服力得多），或者走商业定制。
