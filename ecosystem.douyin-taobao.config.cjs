module.exports = {
  apps: [
    {
      name: "douyin-taobao",
      script: "auto_taobao.py",
      cwd: __dirname,
      interpreter: "/usr/bin/python3",
      autorestart: true,
      restart_delay: 10000,
      kill_timeout: 15000,
      env: {
        PATH: "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        PYTHONUNBUFFERED: "1",
        TAOBAO_WEBUI_PORT: "17861",
      },
    },
  ],
};
