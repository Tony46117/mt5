//+------------------------------------------------------------------+
//| SpotDump.mq5 - live bridge between MT5 and the python scripts    |
//| Attach as an Expert Advisor to any chart (one time). It writes   |
//| MQL5/Files/spots.csv   (bid/ask for EVERY symbol with a quote)   |
//| MQL5/Files/trades.csv  (all open positions + account header row) |
//| MQL5/Files/candles.csv (last 120 M1 OHLC bars of EURUSD+GBPUSD)  |
//| every 50 ms.                                                     |
//|                                                                  |
//| ORDER CHANNEL (v1.40) - python can now TRADE through the bridge: |
//|   python drops ONE FILE per command into MQL5/Files:             |
//|       exec_in.<id>.cmd                                           |
//|   whose content is a TAB separated line:                         |
//|       id<TAB>OPEN<TAB>SYMBOL<TAB>BUY|SELL<TAB>volume             |
//|            [TAB sl TAB tp TAB magic TAB comment]                 |
//|       id<TAB>CLOSE<TAB>ticket                                    |
//|       id<TAB>CLOSEALL<TAB>SYMBOL|ALL                             |
//|       id<TAB>PING                                                |
//|   The EA reads the OLDEST command file, DELETES it (consume) and |
//|   appends a result line to exec_out.csv (raw bytes):             |
//|       id<TAB>OK|ERR<TAB>detail (ticket for OPEN, count for CLOSE)|
//|   exec_out.csv keeps roughly the newest 4 KB of results.  A file |
//|   rename/delete protocol makes lost or duplicated orders         |
//|   impossible on a 50 ms polling loop.                            |
//+------------------------------------------------------------------+
#property copyright "spot bridge"
#property version   "1.40"

#define SPOT_FILE    "spots.csv"
#define TRADES_FILE  "trades.csv"
#define CANDLE_FILE  "candles.csv"
#define EXEC_IN      "exec_in.csv"
#define EXEC_OUT     "exec_out.csv"
#define EXEC_NEXT    "exec_next.txt"   // pointer: filename of newest queued cmd

int        g_interval_ms = 1; // FIXED (non-input): charts can restore stale
                           // saved inputs; a plain global guarantees the
                           // 1 ms tick + sub-5 ms order pickup everywhere
#define IntervalMs g_interval_ms

string      g_sym_names[];    // dynamic Market Watch snapshot
int         g_nsymbols = 0;
long        g_last_rescan_min = 0;   // last Market Watch rescan (minute)
long        g_last_candle_min = 0;   // last candles.csv rewrite (minute)
const long  MAGIC_PY = 777001;       // magic for python-placed orders

// pick a filling mode the symbol actually supports (retcode 10030 otherwise):
// brokers allow FOK and/or IOC per symbol; RETURN for exchange-style.
ENUM_ORDER_TYPE_FILLING FillingFor(string sym)
  {
   long fill = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   if((fill & SYMBOL_FILLING_IOC) != 0)
      return(ORDER_FILLING_IOC);
   if((fill & SYMBOL_FILLING_FOK) != 0)
      return(ORDER_FILLING_FOK);
   return(ORDER_FILLING_RETURN);
  }

int OnInit()
  {
    RefreshSymbols();
    Print("SpotDump: trade_allowed=", TerminalInfoInteger(TERMINAL_TRADE_ALLOWED),
          " mql_allowed=", MQLInfoInteger(MQL_TRADE_ALLOWED),
          " account_trade_allowed=", AccountInfoInteger(ACCOUNT_TRADE_ALLOWED),
          " account_trade_mode=", AccountInfoInteger(ACCOUNT_TRADE_MODE),
          " terminal_trade_allowed=", TerminalInfoInteger(TERMINAL_TRADE_ALLOWED),
          " mql_trade_allowed=", MQLInfoInteger(MQL_TRADE_ALLOWED));
    EventSetMillisecondTimer(IntervalMs);
    DumpSpots();
    DumpTrades();
    DumpCandles();
    return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

// event-driven order pickup: chart-symbol quotes arrive many times per
// second during market hours - drains the order channel between timer
// ticks so latency is bounded by the quote gap, not the timer.
void OnTick()
  {
   ProcessExecIn();
  }

void OnTimer()
  {
   ProcessExecIn();                 // FIRST - order pickup runs 500x/s for
                                    // sub-5 ms command latency
   long now_min = (long)(TimeCurrent() / 60);
   if(now_min != g_last_rescan_min)  // rescan Market Watch each new minute
      RefreshSymbols();
   DumpSpots();                     // quotes every tick - feeds panel prices
   DumpCandles();                   // self-throttled to a new M1 bar
   // trades.csv only when positions changed or every 20th tick (~40 ms):
   // rewriting it every tick starved the order channel on wine.
   static int  s_last_pos   = -1;
   static int  s_since_dump = 0;
   int total = PositionsTotal();
   if(total != s_last_pos || s_since_dump >= 20)
     {
      s_last_pos   = total;
      s_since_dump = 0;
      DumpTrades();
     }
   else
      s_since_dump++;
  }

// ------------------------------------------------------------------
// symbols: everything in Market Watch that has a quote (max 40)
// ------------------------------------------------------------------
void RefreshSymbols()
  {
   g_nsymbols = 0;
   ArrayResize(g_sym_names, 0);
   int total = SymbolsTotal(false);
   ArrayResize(g_sym_names, MathMin(total, 40));
   for(int i = 0; i < total && g_nsymbols < 40; i++)
     {
      string s = SymbolName(i, false);
      if(SymbolInfoDouble(s, SYMBOL_BID) > 0.0)
        {
         g_sym_names[g_nsymbols] = s;
         g_nsymbols++;
        }
     }
   ArrayResize(g_sym_names, g_nsymbols);
   g_last_rescan_min = (long)(TimeCurrent() / 60);
  }

// ------------------------------------------------------------------
// dumps
// ------------------------------------------------------------------
string MscToTime(long msc)
  {
   string s = TimeToString((datetime)(msc / 1000), TIME_DATE | TIME_SECONDS);
   return s + "." + IntegerToString((int)(msc % 1000), 3, '0');
  }

void DumpSpots()
  {
   int handle = FileOpen(SPOT_FILE, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, '\t');
   if(handle == INVALID_HANDLE)
      return;
   for(int i = 0; i < g_nsymbols; i++)
     {
      string s = g_sym_names[i];
      double bid = SymbolInfoDouble(s, SYMBOL_BID);
      double ask = SymbolInfoDouble(s, SYMBOL_ASK);
      if(bid <= 0.0)
         continue;
      int digits = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      long msc = SymbolInfoInteger(s, SYMBOL_TIME_MSC);
      FileWrite(handle, s, DoubleToString(bid, digits),
                DoubleToString(ask, digits), MscToTime(msc));
     }
   FileClose(handle);
  }

void DumpTrades()
  {
   int handle = FileOpen(TRADES_FILE, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, '\t');
   if(handle == INVALID_HANDLE)
      return;

   int total = PositionsTotal();
// header row: NONE + position count + account snapshot
    //   0 NONE, 1 positions, 2 login, 3 server, 4 balance, 5 equity,
    //   6 currency, 7 leverage, 8 margin, 9 free margin, 10 margin level,
    //   11 profit (floating), 12 broker/company, 13 account holder,
    //   14 margin mode, 15 trade mode (0=disabled 1=close 2=full)
    //   16 terminal trade allowed, 17 mql trade allowed, 18 account trade allowed
    FileWrite(handle,
              "NONE",
              IntegerToString(total),
              IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)),
              AccountInfoString(ACCOUNT_SERVER),
              DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
              DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
              AccountInfoString(ACCOUNT_CURRENCY),
              IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)),
              DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2),
              DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2),
              DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL), 2),
              DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2),
              AccountInfoString(ACCOUNT_COMPANY),
              AccountInfoString(ACCOUNT_NAME),
              IntegerToString(AccountInfoInteger(ACCOUNT_MARGIN_MODE)),
              IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE)),
              IntegerToString(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)),
              IntegerToString(MQLInfoInteger(MQL_TRADE_ALLOWED)),
              IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)));
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      long   type   = PositionGetInteger(POSITION_TYPE);
      long   vmul   = (type == POSITION_TYPE_SELL) ? -1 : 1;
      string sym    = PositionGetString(POSITION_SYMBOL);
      int    dg     = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double vopen  = PositionGetDouble(POSITION_PRICE_OPEN);
      double vcur   = PositionGetDouble(POSITION_PRICE_CURRENT);
      double profit = PositionGetDouble(POSITION_PROFIT);
      double swap   = PositionGetDouble(POSITION_SWAP);
      long   msc    = PositionGetInteger(POSITION_TIME_MSC);
      string stype  = (type == POSITION_TYPE_SELL) ? "SELL" : "BUY";
      FileWrite(handle,
                IntegerToString((long)ticket),
                sym,
                stype,
                DoubleToString(volume * vmul, 2),
                DoubleToString(vopen, dg),
                DoubleToString(vcur, dg),
                DoubleToString(profit, 2),
                DoubleToString(swap, 2),
                IntegerToString(PositionGetInteger(POSITION_MAGIC)),
                MscToTime(msc),
                PositionGetString(POSITION_COMMENT));
     }
   FileClose(handle);
  }

// ------------------------------------------------------------------
// candles: last 120 M1 bars for EURUSD + GBPUSD (OHLC + tick volume)
// rewritten only when a new bar opens (cheap) - monitor keeps its own
// history, so gaps never appear in the web/terminal charts.
// ------------------------------------------------------------------
void DumpCandles()
  {
   string pairs[2] = {"EURUSD", "GBPUSD"};
   long now_min = (long)(TimeCurrent() / 60);
   if(now_min == g_last_candle_min)
      return;                       // same M1 bar - nothing new to write
   g_last_candle_min = now_min;

   int handle = FileOpen(CANDLE_FILE, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, '\t');
   if(handle == INVALID_HANDLE)
      return;
   for(int p = 0; p < 2; p++)
     {
      string sym = pairs[p];
      if(!SymbolSelect(sym, true))
         continue;
      MqlRates rates[];
      int n = CopyRates(sym, PERIOD_M1, 0, 120, rates);   // 0 = include forming bar
      for(int i = n - 1; i >= 0; i--)                     // newest first
        {
         long msc = (long)rates[i].time * 1000;
         FileWrite(handle, sym, MscToTime(msc),
                   DoubleToString(rates[i].open, 5),
                   DoubleToString(rates[i].high, 5),
                   DoubleToString(rates[i].low, 5),
                   DoubleToString(rates[i].close, 5),
                   IntegerToString((int)rates[i].tick_volume));
        }
     }
   FileClose(handle);
  }

// ------------------------------------------------------------------
// order channel: consume exec_in.csv, write results to exec_out.csv
// ------------------------------------------------------------------
// append one "id<TAB>status<TAB>detail" line to exec_out.csv (raw bytes so
// the tabs survive; the file keeps roughly the newest 4 KB of results)
void AppendOut(string id, string status, string detail)
  {
   string old = "";
   int h = FileOpen(EXEC_OUT, FILE_BIN | FILE_READ | FILE_WRITE |
                    FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      h = FileOpen(EXEC_OUT, FILE_BIN | FILE_WRITE |
                   FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return;
   uint size = FileSize(h);
   if(size > 0)
     {
      uchar buf[];
      FileSeek(h, 0, SEEK_SET);
      FileReadArray(h, buf, 0, (int)size);
      old = CharArrayToString(buf, 0, (int)size, CP_UTF8);
      if(StringLen(old) > 4000)          // bound the history
        {
         int cut = StringFind(old, "\n", StringLen(old) - 4000);
         if(cut >= 0)
            old = StringSubstr(old, cut + 1);
        }
     }
   string line = id + "\t" + status + "\t" + detail + "\n";
   string all  = old + line;
   uchar out[];
   int nb = StringToCharArray(all, out, 0, WHOLE_ARRAY, CP_UTF8);
   FileSeek(h, 0, SEEK_SET);
   FileWriteArray(h, out, 0, nb);
   FileClose(h);
  }

// EXEC NEXT protocol (no directory enumeration - Wine's FileFindNext
// proved unreliable): python writes the command to exec_in.<id>.txt and
// then atomically renames exec_next.<id>.tmp over exec_next.txt with the
// filename as content.  The EA reads the pointer, executes that one
// command file, deletes both, and repeats.  A sender whose command was
// queued but overtaken simply re-asserts the pointer until its result
// shows up - nothing can get stuck, nothing runs twice (the file is
// always deleted before execution).  NOTE: command files must NOT use
// executable extensions (.cmd/.bat) - MT5's file sandbox refuses to
// open those (err 5002).
void ProcessExecIn()
  {
   for(int i = 0; i < 50; i++)
     {
      if(!FileIsExist(EXEC_NEXT))
         return;
      int hp = FileOpen(EXEC_NEXT, FILE_READ | FILE_TXT | FILE_ANSI |
                        FILE_SHARE_READ | FILE_SHARE_WRITE);
      if(hp == INVALID_HANDLE)
         return;                        // busy - retry next timer tick
      string target = "";
      while(!FileIsEnding(hp))
        {
         string l = FileReadString(hp);
         StringTrimLeft(l);
         StringTrimRight(l);
         if(StringLen(l) > 0)
            target = l;                 // last line wins = newest queued
        }
      FileClose(hp);
      if(StringLen(target) == 0)
        {
         FileDelete(EXEC_NEXT);
         return;
        }
      int hc = FileOpen(target, FILE_READ | FILE_TXT | FILE_ANSI |
                        FILE_SHARE_READ | FILE_SHARE_WRITE);
      if(hc == INVALID_HANDLE)
        {
         // diagnostics: why can we not open a file python just wrote?
         static int dbg2 = 0;
         int err = GetLastError();
         if(dbg2 < 6)
           {
            bool ex1 = FileIsExist(target);
            int h2 = FileOpen(target, FILE_READ | FILE_BIN | FILE_SHARE_READ);
            int h3 = FileOpen(target, FILE_READ | FILE_TXT | FILE_ANSI |
                              FILE_SHARE_READ);
            Print("exec: open FAILED err=", err, " len=", StringLen(target),
                  " [", target, "] isexist=", ex1,
                  " bin=", h2, " txtshare=", h3);
            if(h2 != INVALID_HANDLE)
               FileClose(h2);
            if(h3 != INVALID_HANDLE)
               FileClose(h3);
            dbg2++;
           }
         FileDelete(EXEC_NEXT);         // stale pointer; sender re-asserts
         return;
        }
      string lines[50];
      int n = 0;
      while(!FileIsEnding(hc) && n < 50)
        {
         string l = FileReadString(hc);
         StringTrimLeft(l);
         StringTrimRight(l);
         if(StringLen(l) > 0)
            lines[n++] = l;
        }
      FileClose(hc);
      if(!FileDelete(target))
        {
         Print("exec: delete ", target, " failed err=", GetLastError());
         FileDelete(EXEC_NEXT);
         return;                        // never execute twice
        }
      FileDelete(EXEC_NEXT);
      Print("exec: consumed ", target, " lines=", n);
      for(int k = 0; k < n; k++)
         ExecuteLine(lines[k]);
     }
  }

void ExecuteLine(string line)
  {
   string p[];
   int k = StringSplit(line, '\t', p);
   if(k < 1)
      return;
   StringTrimLeft(p[0]);
   StringTrimRight(p[0]);
   string id = p[0];

   if(k < 2)
     {
      AppendOut(id, "ERR", "malformed");
      return;
     }
   string cmd = p[1];
   StringToUpper(cmd);

   if(cmd == "PING")
     {
      AppendOut(id, "OK", "pong " + IntegerToString((long)TimeLocal()));
      return;
     }

   if(cmd == "OPEN" && k >= 5)
     {
      string sym = p[2];
      if(!SymbolSelect(sym, true))
        {
         AppendOut(id, "ERR", "unknown symbol " + sym);
         return;
        }
      long ptype = (StringToUpper(p[3]) == "SELL") ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      double vol = StringToDouble(p[4]);
      double sl  = (k > 5) ? StringToDouble(p[5]) : 0.0;
      double tp  = (k > 6) ? StringToDouble(p[6]) : 0.0;
      long   mg  = (k > 7) ? StringToInteger(p[7]) : MAGIC_PY;
      string cm  = (k > 8) ? p[8] : "py";
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double price = (ptype == ORDER_TYPE_BUY)
                     ? SymbolInfoDouble(sym, SYMBOL_ASK)
                     : SymbolInfoDouble(sym, SYMBOL_BID);
      if(vol <= 0.0 || price <= 0.0)
        {
         AppendOut(id, "ERR", "bad volume or no quote");
         return;
        }
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action    = TRADE_ACTION_DEAL;
      req.symbol    = sym;
      req.volume    = NormalizeDouble(vol, 2);
      req.type      = (ENUM_ORDER_TYPE)ptype;
      req.price     = NormalizeDouble(price, digits);
      req.sl        = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
      req.tp        = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
      req.magic     = (ulong)mg;
      req.comment   = cm;
      req.deviation = 20;
      req.type_filling = FillingFor(sym);
      if(!OrderSend(req, res) || (res.retcode != TRADE_RETCODE_DONE &&
                                  res.retcode != TRADE_RETCODE_PLACED &&
                                  res.retcode != TRADE_RETCODE_DONE_PARTIAL))
        {
         AppendOut(id, "ERR", "retcode " + IntegerToString((long)res.retcode));
         return;
        }
      // position ticket = the deal's position id, fall back to order ticket
      ulong tk = res.order;
      if(res.deal > 0 && HistoryDealSelect(res.deal))
         tk = (ulong)HistoryDealGetInteger(res.deal, DEAL_POSITION_ID);
      if(tk == 0)
         tk = res.order;
      AppendOut(id, "OK", DoubleToString(res.price, digits) + "|" +
                                IntegerToString((long)tk) + "|" + DoubleToString(res.volume, 2));
      return;
     }

   if(cmd == "CLOSE" && k >= 3)
     {
      ulong ticket = (ulong)StringToInteger(p[2]);
      if(!PositionSelectByTicket(ticket))
        {
         AppendOut(id, "ERR", "no position " + p[2]);
         return;
        }
      string sym  = PositionGetString(POSITION_SYMBOL);
      long   type = PositionGetInteger(POSITION_TYPE);
double vol  = PositionGetDouble(POSITION_VOLUME);
      int digits  = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double price = (type == POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(sym, SYMBOL_BID)   // closing a buy = sell bid
                      : SymbolInfoDouble(sym, SYMBOL_ASK);
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action       = TRADE_ACTION_DEAL;
      req.symbol       = sym;
      req.position     = ticket;
      req.volume       = vol;
      req.type         = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      req.price        = NormalizeDouble(price, digits);
      req.deviation    = 20;
      req.type_filling = FillingFor(sym);
      if(!OrderSend(req, res) || (res.retcode != TRADE_RETCODE_DONE &&
                                  res.retcode != TRADE_RETCODE_PLACED &&
                                  res.retcode != TRADE_RETCODE_DONE_PARTIAL))
        {
         AppendOut(id, "ERR", "retcode " + IntegerToString((long)res.retcode));
         return;
        }
      AppendOut(id, "OK", "closed " + p[2]);
      return;
     }

   if(cmd == "CLOSEALL" && k >= 3)
     {
      string sym = p[2];
      StringToUpper(sym);
      int closed = 0, failed = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || !PositionSelectByTicket(ticket))
            continue;
         string psym = PositionGetString(POSITION_SYMBOL);
         if(sym != "ALL" && psym != sym)
            continue;
         long   type  = PositionGetInteger(POSITION_TYPE);
         double vol   = PositionGetDouble(POSITION_VOLUME);
         int    dg    = (int)SymbolInfoInteger(psym, SYMBOL_DIGITS);
         double price = (type == POSITION_TYPE_BUY)
                        ? SymbolInfoDouble(psym, SYMBOL_BID)
                        : SymbolInfoDouble(psym, SYMBOL_ASK);
         MqlTradeRequest req;
         MqlTradeResult  res;
         ZeroMemory(req);
         ZeroMemory(res);
         req.action       = TRADE_ACTION_DEAL;
         req.symbol       = psym;
         req.position     = ticket;
         req.volume       = vol;
         req.type         = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
         req.price        = NormalizeDouble(price, dg);
         req.deviation    = 20;
         req.type_filling = FillingFor(psym);
         if(OrderSend(req, res) && (res.retcode == TRADE_RETCODE_DONE ||
                                    res.retcode == TRADE_RETCODE_DONE_PARTIAL ||
                                    res.retcode == TRADE_RETCODE_PLACED))
            closed++;
         else
            failed++;
        }
      AppendOut(id, "OK", "closed " + IntegerToString(closed) +
                                " failed " + IntegerToString(failed));
      return;
     }

   AppendOut(id, "ERR", "unknown cmd " + cmd);
  }
//+------------------------------------------------------------------+
