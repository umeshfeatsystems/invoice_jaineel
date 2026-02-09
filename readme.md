venv\scripts\activate
python main.py
streamlit run frontend.py

PM2 setup (keeps API alive):
1. npm install -g pm2
2. pm2 start ecosystem.config.js
3. pm2 status
4. pm2 logs invoice-grn-api
5. pm2 save

Useful PM2 commands:
- pm2 restart invoice-grn-api
- pm2 stop invoice-grn-api
- pm2 delete invoice-grn-api
- pm2 resurrect
