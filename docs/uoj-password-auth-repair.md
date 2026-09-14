# UOJ 密码登录权限修复

症状：备用测评码正常，UOJ 密码登录失败。数据库错误 1142 表明现有测评数据库账号无法查询 `user_info`。仅测试 `SELECT 1` 不能验证登录所需权限。

需由有 MySQL 授权权限的管理员在现有 UOJ 数据库实例中处理。不要创建新账号、更改密码或开放整库权限。先确认测评连接实际使用的数据库及已存在账号的 Host；保留原有授权，仅补充如下列级只读授权（替换尖括号占位内容）：

```sql
GRANT SELECT (username, password, usergroup)
ON `<现有UOJ数据库名>`.`user_info`
TO 'quiz_reader'@'<现有账号Host>';
```

`password` 为现有 UOJ 存储的密码摘要。该权限用于比对浏览器按 UOJ 协议计算的摘要；不允许写入 UOJ，也不需要读取邮件等其他个人信息字段。不要授予 INSERT、UPDATE、DELETE、GRANT OPTION 或整库 SELECT。

授权后，运行现有维护命令 `check-uoj`；它会在启用密码登录时检查认证查询的权限，使用 `WHERE 1=0`，不读取或输出学生记录。再用本人已知密码进行一次实际登录，勿在聊天或日志中提供密码或摘要。测评维护白名单没有数据库授权入口，因此不能由当前维护账号直接执行 GRANT。
