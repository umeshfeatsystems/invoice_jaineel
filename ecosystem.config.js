module.exports = {
  apps: [
    {
      name: "invoice-grn-api",
      cwd: "./",
      script: "venv/Scripts/python.exe",
      args: "-m uvicorn main:app --host 0.0.0.0 --port 5612",
      interpreter: "none",
      autorestart: true,
      max_restarts: 20,
      min_uptime: "10s",
      restart_delay: 5000,
      watch: false,
      time: true,
      env: {
        PYTHONUNBUFFERED: "1"
      }
    }
  ]
};
