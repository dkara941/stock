# 部署到 Streamlit Community Cloud(免費,任何人掃 QR 都能開)

## 資料夾內容
- stock_comp.py     主程式
- requirements.txt  Python 套件
- packages.txt      系統套件(chromium、chromium-driver),雲端會自動 apt 安裝

## 步驟
1. 在 GitHub 建一個 repo,把這個資料夾的 4 個檔案放在 repo 根目錄後 push。
2. 開 https://share.streamlit.io ,用 GitHub 登入 → Create app。
3. 選該 repo、Main file path 填 `stock_comp.py` → Deploy。
4. 第一次建置約數分鐘(要裝 chromium)。完成後會得到 https://xxxx.streamlit.app 。
5. 用手機/平板開那個網址,或在電腦開啟後用左側欄的 QR code 掃描。
   QR code 會自動使用目前的公開網址。

## 本機測試
    pip install -r requirements.txt
    streamlit run stock_comp.py --server.address 0.0.0.0

## 常見問題
- 價量/籌碼抓不到:證交所、櫃買可能擋海外機房 IP,請看 App 右下角 Manage app → Logs。
- ETF 成分股失敗:看 Logs 是否為 chromium 啟動錯誤;可稍後重試(免費方案記憶體有限)。
- 久沒人用會休眠,首次開啟需等它喚醒。
- 網址是公開的;如需限制,在 App 設定 → Sharing 只允許指定 email。
