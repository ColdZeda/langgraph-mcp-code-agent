-- 只读账号：给「只读工具」用的数据库层兜底（主防线是应用层的语句白名单，见 mysql_tools.py）
--
-- 说明：
--   * 这个脚本只在 MySQL **首次初始化数据目录**时执行（docker-entrypoint-initdb.d 的机制）；
--     之后改这里不会自动生效，需要手动执行或删卷重建。
--   * 阶段 1 是手动建的这个账号，阶段 2 把它落成脚本，避免"换台机器就丢了"。
--   * 密码是本地开发用的弱口令，与 .env.example 里的模板一致；真实部署请改掉。
CREATE USER IF NOT EXISTS 'agent_readonly'@'%' IDENTIFIED BY 'readonly_pwd';
GRANT SELECT ON agent_test.* TO 'agent_readonly'@'%';
FLUSH PRIVILEGES;
