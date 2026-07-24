# Fincept Web Terminal

一個橫跨台股 / 美股 / 加密貨幣的金融終端;AI 人物(Persona)研究標的、
互相辯論,產出可追蹤的交易建議,系統事後再以真實價格為其打分。

## AI 研究與討論

**Discussion**:
針對一或多檔標的的多人物 AI 圓桌;分回合進行、每回合計分,最後由
synthesizer 收斂出一份 Conclusion。這是產品的核心研究產物。
_Avoid_: chat、辯論、thread、對話串

**Chat**:
與「單一」Persona 的一對一串流對話(AI 頁面)。與 Discussion 區別:後者
是多人物、且會產出 Conclusion。
_Avoid_: conversation、agent chat

**Persona**:
帶有固定投資風格與 prompt 的 AI agent;是 Discussion 的與會者、也是 Chat
的對談對象。
_Avoid_: agent、bot、model、角色

**Conclusion**:
synthesizer 為一場 Discussion 產出的最終結構化結果——交易建議加上其理由。
_Avoid_: summary、結論摘要、答案

**Verdict**:
一場 Discussion 建議的「自評終局結果」,取值 `win` / `loss` / `unverifiable`。
這是對「這個建議是否應驗」的判定,**不是損益數字**——`loss` 不代表虧錢。
_Avoid_: outcome、損益、score

**Scoreboard**:
一場 Discussion 所推薦標的的 D1–D5 收盤價追蹤記錄,用來判斷該建議事後
幾天的表現。
_Avoid_: 排行榜、結果表

## 回測與學習

**Replay**(即 as-of / backtest 模式):
把一場 Discussion 當成「過去某日」來跑,讓 AI 只看得到當時可得的資料;
這是拿建議去對照已知結果打分的基礎。
_Avoid_: 模擬、historical run

**Sweep**:
在一段日期區間內批次跑多場回測 Discussion,用以量測某策略的命中率。
_Avoid_: batch、campaign

**Walk-forward**:
把一個 Sweep 切成滾動的 train/test 折(fold),確保學到的權重只會用在
樣本外。
_Avoid_: 交叉驗證

**Post-mortem**:
在一場 Discussion 的結果揭曉後,對它做的結構化自我檢討,含「缺什麼資料」
的分類。
_Avoid_: retro、事後檢討

**Lesson**:
從 post-mortem 萃取出的一條學習。*episodic*(情節)綁定單一 Discussion;
一旦跨多場歸納成立,便晉升為 *semantic*(語意)。
_Avoid_: insight、筆記、心得

**Calibration**:
把模型的「原始信心」映射到「經驗校正後信心」的曲線,由過往
(信心, 結果)配對擬合而成。
_Avoid_: tuning、調整

**Signal audit**:
逐一查核一場 Discussion 引用的每個數字是否真實、取值是否正確——用以揪出
幻覺或引錯的資料。
_Avoid_: fact-check、驗證

## 市場資料

**Waterfall**:
按序嘗試各資料供應商、直到有一家回傳資料的鏈
(台股:TWSE → FinMind → MOPS;美股:Polygon → yfinance → Stooq → Finnhub)。
_Avoid_: fallback chain、cascade、瀑布

**Sponsor token**:
系統用來讀取台股資料的那把「共用 FinMind 付費層」憑證。
_Avoid_: API key、授權

**Universe**:
每日排程自行探索並據以運作的標的集合(台股標的取自 stock-info 表)。
_Avoid_: 標的清單、watchlist

**Chip flow**(主力分點 / broker concentration):
以標的為單位,彙整哪些券商分點在淨買/淨賣,作為台股法人資金訊號。
_Avoid_: order flow、法人流向

**Regime**:
市場狀態的分級(tier);lesson 與 calibration 會依此分桶,使指引因 regime
而異。
_Avoid_: 市場狀態、階段

## 投資組合與存取

**Holding**:
一個 Portfolio 中「目前持有」的部位,與產生它的 Transaction 交易歷史有別。
_Avoid_: position、資產、持股

**Thesis**:
以 owner 為範圍、長期存在的某標的投資論點,附審視與事件時間軸。與
Discussion(一次性)、Lesson(習得)有別。
_Avoid_: idea、想法、筆記

**Customer key**:
發給「外部資料消費者」的 API key,與瀏覽器登入(JWT session)不同。
_Avoid_: token、API key(語意含糊)

**Owner-scoped**:
僅該站台唯一 owner/admin 可見的資料,相對於開放給 customer-key 持有者的
資料。
_Avoid_: private、admin-only
