
#property copyright "spot bridge"
#property version   "1.77"

#define SPOT_FILE    "spots.csv"
#define TRADES_FILE  "trades.csv"
#define ORDERS_FILE  "orders.csv"
#define CANDLE_FILE  "candles.csv"
#define SYMBOLS_FILE "symbols.csv"
#define EXEC_IN      "exec_in.csv"
#define EXEC_OUT     "exec_out.csv"
#define EXEC_NEXT    "exec_next.txt"
#define SPOT_DUMP_MS 50
#define ASYNC_MAX    64
// Watchlist dumped to spots.csv (Market Watch + classic pairs + Deriv
// synthetics).  Kept modest on purpose: this file feeds the live panel.
#define MAX_SYM_DUMP 300
// Full broker catalogue dumped to symbols.csv (EVERY symbol this account
// can see, whatever the broker calls it).  Written rarely and consumed by
// broker_prober, so it can be large without costing the price feed.
#define MAX_SYM_CATALOGUE 12000
#define SYMBOL_DUMP_SEC 60

int        g_interval_ms = 1;

struct AsyncReq
  {
   ulong      req_id;
   string     cmd_id;
   bool       is_open;
   string     sym;
   double     vol;
   int        digits;
   long       fill;
   MqlTradeRequest req;
  };
AsyncReq  g_async[ASYNC_MAX];
int       g_nasync = 0;

void AsyncTrack(ulong req_id, string cmd_id, bool is_open,
                string sym, double vol, int digits, long fill,
                MqlTradeRequest &req)
  {
   for(int i = 0; i < g_nasync; i++)
      if(g_async[i].req_id == req_id)
        {
         g_async[i].fill = fill;
         return;
        }
   if(g_nasync >= ASYNC_MAX)
     {
      for(int i = 1; i < ASYNC_MAX; i++) g_async[i - 1] = g_async[i];
      g_nasync = ASYNC_MAX - 1;
     }
   g_async[g_nasync].req_id  = req_id;
   g_async[g_nasync].cmd_id  = cmd_id;
   g_async[g_nasync].is_open = is_open;
   g_async[g_nasync].sym     = sym;
   g_async[g_nasync].vol     = vol;
   g_async[g_nasync].digits  = digits;
   g_async[g_nasync].fill    = fill;
   g_async[g_nasync].req     = req;
   g_nasync++;
  }

void AsyncForget(ulong req_id)
  {
   for(int i = 0; i < g_nasync; i++)
      if(g_async[i].req_id == req_id)
        {
         for(int k = i + 1; k < g_nasync; k++) g_async[k - 1] = g_async[k];
         g_nasync--;
         return;
        }
  }

int AsyncIndex(ulong req_id)
  {
   for(int i = 0; i < g_nasync; i++)
      if(g_async[i].req_id == req_id)
         return(i);
   return(-1);
  }

void AsyncReport(int i, bool ok, string detail)
  {
   AppendOut(g_async[i].cmd_id, ok ? "OK" : "ERR", detail);
   AsyncForget(g_async[i].req_id);
  }
#define IntervalMs g_interval_ms

string      g_sym_names[];
int         g_nsymbols = 0;
long        g_last_rescan_min = 0;
long        g_last_candle_min = 0;
const long  MAGIC_PY = 777001;

#define MAX_PROVEN 64
string                       g_proven_sym[MAX_PROVEN];
ENUM_ORDER_TYPE_FILLING      g_proven_fill[MAX_PROVEN];
int                          g_nproven = 0;

void ProvenFillRemember(string sym, ENUM_ORDER_TYPE_FILLING f)
  {
   for(int i = 0; i < g_nproven; i++)
      if(g_proven_sym[i] == sym) { g_proven_fill[i] = f; return; }
   if(g_nproven < MAX_PROVEN)
     {
      g_proven_sym[g_nproven]  = sym;
      g_proven_fill[g_nproven] = f;
      g_nproven++;
     }
  }

ENUM_ORDER_TYPE_FILLING ProvenFill(string sym)
  {
   for(int i = 0; i < g_nproven; i++)
      if(g_proven_sym[i] == sym)
         return(g_proven_fill[i]);
   return((ENUM_ORDER_TYPE_FILLING)0);
  }

ENUM_ORDER_TYPE_FILLING FillingFor(string sym)
  {
   long fill = SymbolInfoInteger(sym, SYMBOL_FILLING_MODE);
   if((fill & SYMBOL_FILLING_IOC) != 0)
      return(ORDER_FILLING_IOC);
   if((fill & SYMBOL_FILLING_FOK) != 0)
      return(ORDER_FILLING_FOK);
   return(ORDER_FILLING_RETURN);
  }

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_REQUEST)
      return;

   int i = AsyncIndex(result.request_id);
   if(i < 0)
      return;
   if(result.retcode == TRADE_RETCODE_DONE ||
      result.retcode == TRADE_RETCODE_PLACED ||
      result.retcode == TRADE_RETCODE_DONE_PARTIAL)
     {
      ProvenFillRemember(g_async[i].sym, (ENUM_ORDER_TYPE_FILLING)g_async[i].fill);

      ulong tk = result.order;
      if(result.deal > 0 && HistoryDealSelect(result.deal))
         tk = (ulong)HistoryDealGetInteger(result.deal, DEAL_POSITION_ID);
      if(tk == 0)
         tk = result.order;
      if(g_async[i].is_open)
         AsyncReport(i, true,
                     DoubleToString(result.price, g_async[i].digits) + "|" +
                     IntegerToString((long)tk) + "|" +
                     DoubleToString(result.volume, 2));
      else
         AsyncReport(i, true, "closed " + g_async[i].sym);
     }
   else if(result.retcode == TRADE_RETCODE_INVALID_FILL)
     {

      ENUM_ORDER_TYPE_FILLING nxt = NextFilling((ENUM_ORDER_TYPE_FILLING)g_async[i].fill);
      if(nxt != 0)
        {
         MqlTradeRequest r2 = g_async[i].req;
         MqlTradeResult  res2;
         ZeroMemory(res2);
         r2.type_filling = nxt;
         if(OrderSendAsync(r2, res2) && res2.request_id != 0)
           {
            g_async[i].req_id = res2.request_id;
            g_async[i].fill   = (long)nxt;
            g_async[i].req    = r2;
           }
         else
            AsyncReport(i, false, "retcode 10030");
        }
      else
         AsyncReport(i, false, "retcode 10030");
     }
   else
      AsyncReport(i, false, "retcode " + IntegerToString((long)result.retcode));
  }

ENUM_ORDER_TYPE_FILLING NextFilling(ENUM_ORDER_TYPE_FILLING f)
  {
   if(f == ORDER_FILLING_FOK)  return(ORDER_FILLING_IOC);
   if(f == ORDER_FILLING_IOC)  return(ORDER_FILLING_RETURN);
   return((ENUM_ORDER_TYPE_FILLING)0);
  }

double NormalizeVolume(string sym, double vol)
  {
   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = 0.01;
   double v = MathRound(vol / vstep) * vstep;
   if(vmin > 0.0 && v < vmin) v = vmin;
   if(vmax > 0.0 && v > vmax) v = vmax;

   return(NormalizeDouble(v, 8));
  }

bool SendDealWithFallback(MqlTradeRequest &req, MqlTradeResult &res)
  {
   ENUM_ORDER_TYPE_FILLING f = ProvenFill(req.symbol);
   if(f == 0) f = FillingFor(req.symbol);
   ENUM_ORDER_TYPE_FILLING tried[4];
   int ntried = 0;
   while(true)
     {
      req.type_filling = f;
      bool sent = OrderSend(req, res);
      if(sent && (res.retcode == TRADE_RETCODE_DONE ||
                  res.retcode == TRADE_RETCODE_PLACED ||
                  res.retcode == TRADE_RETCODE_DONE_PARTIAL))
        {
         ProvenFillRemember(req.symbol, f);
         return(true);
        }

      if(res.retcode != TRADE_RETCODE_INVALID_FILL)
         return(false);
      ENUM_ORDER_TYPE_FILLING nxt = NextFilling(f);
      for(int t = 0; t < ntried; t++)
         if(tried[t] == nxt) nxt = (ENUM_ORDER_TYPE_FILLING)0;
      if(nxt == 0)
         return(false);
      tried[ntried++] = f;
      f = nxt;
     }
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
    Print("SpotDump: balance=", DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2),
          " equity=", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2),
          " currency=", AccountInfoString(ACCOUNT_CURRENCY),
          " leverage=", AccountInfoInteger(ACCOUNT_LEVERAGE),
          " margin=", DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2),
          " margin_free=", DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2),
          " profit=", DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2),
          " company=", AccountInfoString(ACCOUNT_COMPANY),
          " name=", AccountInfoString(ACCOUNT_NAME),
          " margin_mode=", AccountInfoInteger(ACCOUNT_MARGIN_MODE));
    EventSetMillisecondTimer(IntervalMs);
    DumpSpots();
    DumpTrades();
    DumpOrders();
    DumpCandles();
    DumpSymbols();
    return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTick()
  {
   ProcessExecIn();
  }

void OnTimer()
  {
   ProcessExecIn();

   long now_min = (long)(TimeCurrent() / 60);
   if(now_min != g_last_rescan_min)
      RefreshSymbols();

   static uint s_last_spot_ms = 0;
   uint now_ms = GetTickCount();
   if(now_ms - s_last_spot_ms >= SPOT_DUMP_MS)
     {
      s_last_spot_ms = now_ms;
      DumpSpots();
     }
   DumpCandles();

   static int  s_last_pos   = -1;
   static int  s_last_ord   = -1;
   static int  s_since_dump = 0;
   int total = PositionsTotal();
   int nords = OrdersTotal();
   if(total != s_last_pos || nords != s_last_ord || s_since_dump >= 20)
     {
      s_last_pos   = total;
      s_last_ord   = nords;
      s_since_dump = 0;
      DumpTrades();
      DumpOrders();
     }
   else
      s_since_dump++;

   // Full broker catalogue: once every SYMBOL_DUMP_SEC, plus once right
   // after a login changes (handled by OnInit), so the prober always has
   // the account's complete quote universe without a single exec round
   // trip.
   static uint s_last_cat_ms = 0;
   if(now_ms - s_last_cat_ms >= SYMBOL_DUMP_SEC * 1000)
     {
      s_last_cat_ms = now_ms;
      DumpSymbols();
     }
  }

bool IsClassicPair(string name)
  {
    static string bases[] = {
       "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD",
       "EURGBP","EURJPY","GBPJPY","AUDJPY","CHFJPY","EURCHF","EURAUD",
       "GBPCHF","CADJPY","AUDNZD","GBPAUD","EURNZD","AUDCAD","NZDJPY",
       "GBPNZD","EURCAD","GBPCAD","AUDCHF","NZDCHF","CADCHF",
       "XAUUSD","XAGUSD",
       // Deriv synthetics (Boom/Crash/Jump/Volatility/Step/RangeBreak):
       // prefix-matched, so they are dumped, probed and tradeable even
       // when the operator never added them to Market Watch.
       "BOOM","CRASH","JUMP","VOLATILITY","STEP INDEX","RANGE BREAK"};
   int n = ArraySize(bases);
   for(int i = 0; i < n; i++)
      if(StringFind(name, bases[i]) == 0)
         return(true);
   return(false);
  }

void SymAdd(string s)
  {
   if(StringLen(s) == 0 || g_nsymbols >= MAX_SYM_DUMP)
      return;
   for(int i = 0; i < g_nsymbols; i++)
      if(g_sym_names[i] == s)
         return;
   // SUBSCRIBE FIRST.  A symbol this terminal has never selected (a Deriv
   // Boom/Crash/Jump index, a cent-account variant, an exotic) reports
   // bid 0 until MT5 selects it - the old "skip when there is no bid yet"
   // test therefore dropped exactly the symbols the account exists for.
   // Select it, then keep it: the quote lands a tick later and DumpSpots
   // starts writing it from then on.
   if(!SymbolSelect(s, true))
      return;
   g_sym_names[g_nsymbols] = s;
   g_nsymbols++;
  }

void RefreshSymbols()
  {
   ArrayResize(g_sym_names, MAX_SYM_DUMP);
   g_nsymbols = 0;

   // 1) every symbol the operator already watches (Market Watch) first
   int mw = SymbolsTotal(true);
   for(int i = 0; i < mw && g_nsymbols < MAX_SYM_DUMP; i++)
      SymAdd(SymbolName(i, true));

   // 2) then EVERY broker variant of the classic pairs, whatever letters the
   //    broker appends (EURUSDc, EURUSD.raw, XAUUSD247, EURUSD#, ...).  This
   //    is what a cent/ECN/standard feed needs: the panel and the broker
   //    prober see each account's own quotes instead of only Market Watch.
   int all = SymbolsTotal(false);
   for(int i = 0; i < all && g_nsymbols < MAX_SYM_DUMP; i++)
     {
      string s = SymbolName(i, false);
      string up = s;
      StringToUpper(up);
      if(IsClassicPair(up))
         SymAdd(s);
     }

   ArrayResize(g_sym_names, g_nsymbols);
   g_last_rescan_min = (long)(TimeCurrent() / 60);
  }

// Every symbol this account can see, with the FULL capability set the
// broker prober needs (same fields the PROBE command returns, plus the
// live bid/ask so consumers can tell which names are actually quoting).
// This is what lets the software probe ALL of a broker's quotes - Deriv's
// Boom/Crash/Jump/Volatility indices, cent-account variants, suffixed ECN
// pairs - without a dedicated terminal install and without subscribing
// thousands of symbols into Market Watch.
void DumpSymbols()
  {
   // Publish only while the trade account is actually AUTHORIZED.  A
   // terminal that is still connecting - or whose login was refused -
   // exposes MetaQuotes' GLOBAL symbol base (thousands of NASDAQ/ETF
   // tickers this account cannot trade) instead of its broker's list, and
   // dumping that would swamp the prober and the panel with instruments
   // that do not belong to this account.  Leaving the previous file alone
   // keeps the last known-good catalogue readable.
   if(StringLen(AccountInfoString(ACCOUNT_CURRENCY)) == 0)
      return;
   int handle = FileOpen(SYMBOLS_FILE, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, '\t');
   if(handle == INVALID_HANDLE)
      return;
   int all = SymbolsTotal(false);
   int written = 0;
   for(int i = 0; i < all && written < MAX_SYM_CATALOGUE; i++)
     {
      string s = SymbolName(i, false);
      if(StringLen(s) == 0)
         continue;
      int digits = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      FileWrite(handle,
                s,
                IntegerToString(digits),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_FILLING_MODE)),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_TRADE_EXEMODE)),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_TRADE_MODE)),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_ORDER_MODE)),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL)),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_TRADE_FREEZE_LEVEL)),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 2),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 2),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 2),
                IntegerToString(SymbolInfoInteger(s, SYMBOL_SPREAD)),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE), 2),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE), 8),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE), 8),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_BID), digits),
                DoubleToString(SymbolInfoDouble(s, SYMBOL_ASK), digits));
      written++;
     }
   FileClose(handle);
  }

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
                 DoubleToString(volume, 2),
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

// Pending stop/limit orders, so the panel can SHOW the StopExe straddle
// and the red CLOSE bar can DELETE it.  orders.csv rows:
// ticket, symbol, type (BUY LIMIT / BUY STOP / SELL LIMIT / SELL STOP),
// volume, price_open, sl, tp, magic, time_setup_msc, comment.
// A "NONE count" header mirrors trades.csv so readers can tell an empty
// book apart from a missing file.
void DumpOrders()
  {
   int handle = FileOpen(ORDERS_FILE, FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, '\t');
   if(handle == INVALID_HANDLE)
      return;
   int total = OrdersTotal();
   FileWrite(handle, "NONE", IntegerToString(total));
   for(int i = total - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket))
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      long otype = OrderGetInteger(ORDER_TYPE);
      string tname = "UNKNOWN";
      if(otype == ORDER_TYPE_BUY_LIMIT)       tname = "BUY LIMIT";
      else if(otype == ORDER_TYPE_BUY_STOP)   tname = "BUY STOP";
      else if(otype == ORDER_TYPE_SELL_LIMIT) tname = "SELL LIMIT";
      else if(otype == ORDER_TYPE_SELL_STOP)  tname = "SELL STOP";
      else if(otype == ORDER_TYPE_BUY_STOP_LIMIT)  tname = "BUY STOP LIMIT";
      else if(otype == ORDER_TYPE_SELL_STOP_LIMIT) tname = "SELL STOP LIMIT";
      FileWrite(handle,
                IntegerToString((long)ticket),
                sym,
                tname,
                DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 2),
                DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), dg),
                DoubleToString(OrderGetDouble(ORDER_SL), dg),
                DoubleToString(OrderGetDouble(ORDER_TP), dg),
                IntegerToString(OrderGetInteger(ORDER_MAGIC)),
                MscToTime((long)OrderGetInteger(ORDER_TIME_SETUP_MSC)),
                OrderGetString(ORDER_COMMENT));
     }
   FileClose(handle);
  }

void DumpCandles()
  {
   string pairs[2] = {"EURUSD", "GBPUSD"};
   long now_min = (long)(TimeCurrent() / 60);
   if(now_min == g_last_candle_min)
      return;
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
      int n = CopyRates(sym, PERIOD_M1, 0, 120, rates);
      for(int i = n - 1; i >= 0; i--)
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
      if(StringLen(old) > 4000)
        {
         // Do NOT trim.  Trimming shifts every byte, so the Python reader's
         // byte offset no longer points at fresh data and every result after
         // the trim is missed.  Dropping the history makes the file much
         // smaller - a shrink the reader detects and re-reads from scratch.
         old = "";
        }
     }
   string line = id + "\t" + status + "\t" + detail + "\n";
   string all  = old + line;
   uchar out[];
   int nb = StringToCharArray(all, out, 0, WHOLE_ARRAY, CP_UTF8);
   // StringToCharArray appends a terminating zero.  Writing it put a NUL
   // byte after every line, which shifted each later line one byte left
   // relative to the reader's byte offset - so results were appended but
   // never seen.  Write the text only.
   if(nb > 0)
      nb -= 1;
   // MQL5 cannot truncate a file, so an in-place rewrite left stale bytes
   // behind whenever the content got shorter - that eventually corrupted
   // this file and stalled every command on it.  Recreate the file instead:
   // its size always matches its content, and a shrink is exactly the signal
   // the reader uses to reset its byte offset.
   FileClose(h);
   FileDelete(EXEC_OUT);
   int hw = FileOpen(EXEC_OUT, FILE_BIN | FILE_WRITE |
                     FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(hw == INVALID_HANDLE)
      return;
   FileWriteArray(hw, out, 0, nb);
   FileClose(hw);
  }

// Read one command file, delete it, then run its lines.  The file is
// deleted BEFORE the lines execute so a command can never be double-fired.
bool ConsumeCommandFile(string target)
  {
   int hc = FileOpen(target, FILE_READ | FILE_TXT | FILE_ANSI |
                     FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(hc == INVALID_HANDLE)
      return(false);
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
      return(false);
   for(int k = 0; k < n; k++)
      ExecuteLine(lines[k]);
   return(true);
  }

// The pointer file is a PRIORITY hint: consume the file Python pointed at
// first so the oldest command is never starved behind a flood of new ones.
bool ConsumePointer()
  {
   if(!FileIsExist(EXEC_NEXT))
      return(false);
   int hp = FileOpen(EXEC_NEXT, FILE_READ | FILE_TXT | FILE_ANSI |
                     FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(hp == INVALID_HANDLE)
      return(false);
   string target = "";
   while(!FileIsEnding(hp))
     {
      string l = FileReadString(hp);
      StringTrimLeft(l);
      StringTrimRight(l);
      if(StringLen(l) > 0)
         target = l;
     }
   FileClose(hp);
   FileDelete(EXEC_NEXT);
   if(StringLen(target) == 0 || !FileIsExist(target))
      return(false);
   return(ConsumeCommandFile(target));
  }

// Drain the directory: any other command file that is fully written is
// consumed in the SAME pass.  Before this the terminal ran exactly one
// command per pointer handshake (Python had to re-point every ~1 ms), so
// a 50-position basket cost 50 handshakes before the first fill even
// started.  Python writes each command to a temp name and renames it, so
// a file found here is always complete.
bool ConsumePending()
  {
   string found = "";
   long h = FileFindFirst("exec_in.*.txt", found);
   if(h == INVALID_HANDLE)
      return(false);
   bool did = false;
   int guard = 0;
   do
     {
      guard++;
      if(StringFind(found, "exec_in.") != 0)
         break;
      if(ConsumeCommandFile(found))
         did = true;
     }
   while(guard < 64 && FileFindNext(h, found));
   FileFindClose(h);
   return(did);
  }

void ProcessExecIn()
  {
   for(int i = 0; i < 50; i++)
     {
      bool did = ConsumePointer();
      if(!did)
         did = ConsumePending();
      if(!did)
         return;
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

      string sidestr = p[3];
      StringToUpper(sidestr);
      long ptype = (sidestr == "SELL") ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
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
      req.volume    = NormalizeVolume(sym, vol);
      req.type      = (ENUM_ORDER_TYPE)ptype;
      req.price     = NormalizeDouble(price, digits);
      req.sl        = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
      req.tp        = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
      req.magic     = (ulong)mg;
      req.comment   = cm;
      req.deviation = 20;
      ENUM_ORDER_TYPE_FILLING f = ProvenFill(sym);
      if(f == 0) f = FillingFor(sym);
      req.type_filling = f;
      MqlTradeResult ares;
      ZeroMemory(ares);
      if(!OrderSendAsync(req, ares) || ares.retcode == 0 ||
         (ares.retcode != TRADE_RETCODE_PLACED &&
          ares.retcode != TRADE_RETCODE_DONE &&
          ares.retcode != TRADE_RETCODE_DONE_PARTIAL))
        {

         AppendOut(id, "ERR", "retcode " + IntegerToString((long)ares.retcode));
         return;
        }
      AsyncTrack(ares.request_id, id, true, sym, req.volume, digits, (long)f, req);
      return;
     }

   // STOPEXE\tsym\tvol\tbuyprice\tsellprice\t[magic]\t[comment]
   // One file, one pass, both legs back-to-back: the straddle rests on
   // the broker TOGETHER (a two-file batch could split across passes).
   // Either both legs ack or neither stands: if the sell leg fails after
   // the buy leg is placed, the buy leg is pulled before replying.
   if(cmd == "STOPEXE" && k >= 6)
     {
      string sym = p[2];
      if(!SymbolSelect(sym, true))
        {
         AppendOut(id, "ERR", "unknown symbol " + sym);
         return;
        }
      double vol   = NormalizeVolume(sym, StringToDouble(p[3]));
      double bprice = StringToDouble(p[4]);
      double sprice = StringToDouble(p[5]);
      long   mg    = (k > 6) ? StringToInteger(p[6]) : MAGIC_PY;
      string cm    = (k > 7) ? p[7] : "stopexe";
      int digits   = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      double ask   = SymbolInfoDouble(sym, SYMBOL_ASK);
      double bid   = SymbolInfoDouble(sym, SYMBOL_BID);
      bprice = NormalizeDouble(bprice, digits);
      sprice = NormalizeDouble(sprice, digits);
      if(vol <= 0.0 || bprice <= 0.0 || sprice <= 0.0 || ask <= 0.0 || bid <= 0.0)
        {
         AppendOut(id, "ERR", "bad volume, price or no quote");
         return;
        }
      double stops  = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * point;
      double freeze = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL) * point;
      double gap    = MathMax(stops, freeze);
      if(bprice < ask + gap)
        {
         AppendOut(id, "ERR", "buy stop must sit ABOVE the ask (bid " +
                   DoubleToString(bid, digits) + " ask " +
                   DoubleToString(ask, digits) + ")");
         return;
        }
      if(sprice > bid - gap)
        {
         AppendOut(id, "ERR", "sell stop must sit BELOW the bid (bid " +
                   DoubleToString(bid, digits) + " ask " +
                   DoubleToString(ask, digits) + ")");
         return;
        }
      MqlTradeRequest breq;
      MqlTradeResult  bres;
      ZeroMemory(breq);
      ZeroMemory(bres);
      breq.action       = TRADE_ACTION_PENDING;
      breq.symbol       = sym;
      breq.volume       = vol;
      breq.type         = ORDER_TYPE_BUY_STOP;
      breq.price        = bprice;
      breq.magic        = (ulong)mg;
      breq.comment      = cm;
      breq.type_time    = ORDER_TIME_GTC;
      breq.type_filling = ORDER_FILLING_RETURN;
      if(!OrderSend(breq, bres) ||
         (bres.retcode != TRADE_RETCODE_DONE && bres.retcode != TRADE_RETCODE_PLACED))
        {
         AppendOut(id, "ERR", "buy stop retcode " +
                   IntegerToString((long)bres.retcode) + " " + bres.comment);
         return;
        }
      MqlTradeRequest sreq;
      MqlTradeResult  sres;
      ZeroMemory(sreq);
      ZeroMemory(sres);
      sreq.action       = TRADE_ACTION_PENDING;
      sreq.symbol       = sym;
      sreq.volume       = vol;
      sreq.type         = ORDER_TYPE_SELL_STOP;
      sreq.price        = sprice;
      sreq.magic        = (ulong)mg;
      sreq.comment      = cm;
      sreq.type_time    = ORDER_TIME_GTC;
      sreq.type_filling = ORDER_FILLING_RETURN;
      if(!OrderSend(sreq, sres) ||
         (sres.retcode != TRADE_RETCODE_DONE && sres.retcode != TRADE_RETCODE_PLACED))
        {
         // Pull the buy leg so a half straddle never stands alone.
         MqlTradeRequest dreq;
         MqlTradeResult  dres;
         ZeroMemory(dreq);
         ZeroMemory(dres);
         dreq.action = TRADE_ACTION_REMOVE;
         dreq.order  = bres.order;
         OrderSend(dreq, dres);
         DumpOrders();
         AppendOut(id, "ERR", "sell stop retcode " +
                   IntegerToString((long)sres.retcode) + " " + sres.comment +
                   " - buy leg #" + IntegerToString((long)bres.order) + " pulled");
         return;
        }
      DumpOrders();
      AppendOut(id, "OK", DoubleToString(bprice, digits) + "|" +
                IntegerToString((long)bres.order) + "|" +
                DoubleToString(sprice, digits) + "|" +
                IntegerToString((long)sres.order));
      return;
     }

   // PENDING\tsym\tBUYLIMIT|BUYSTOP|SELLLIMIT|SELLSTOP\tvol\tprice\t[sl]\t[tp]\t[magic]\t[comment]
   // Placed synchronously (no async tracking) so the ticket comes straight
   // back to the caller - a pending ticket is the whole point of the reply.
   if(cmd == "PENDING" && k >= 6)
     {
      string sym = p[2];
      if(!SymbolSelect(sym, true))
        {
         AppendOut(id, "ERR", "unknown symbol " + sym);
         return;
        }
      string tstr = p[3];
      StringToUpper(tstr);
      long ptype = -1;
      if(tstr == "BUYLIMIT")        ptype = ORDER_TYPE_BUY_LIMIT;
      else if(tstr == "BUYSTOP")    ptype = ORDER_TYPE_BUY_STOP;
      else if(tstr == "SELLLIMIT")  ptype = ORDER_TYPE_SELL_LIMIT;
      else if(tstr == "SELLSTOP")   ptype = ORDER_TYPE_SELL_STOP;
      if(ptype < 0)
        {
         AppendOut(id, "ERR", "bad pending type " + tstr);
         return;
        }
      double vol   = StringToDouble(p[4]);
      double price = StringToDouble(p[5]);
      double sl    = (k > 6) ? StringToDouble(p[6]) : 0.0;
      double tp    = (k > 7) ? StringToDouble(p[7]) : 0.0;
      long   mg    = (k > 8) ? StringToInteger(p[8]) : MAGIC_PY;
      string cm    = (k > 9) ? p[9] : "pend";
      int digits   = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double point = SymbolInfoDouble(sym, SYMBOL_POINT);
      double ask   = SymbolInfoDouble(sym, SYMBOL_ASK);
      double bid   = SymbolInfoDouble(sym, SYMBOL_BID);
      price        = NormalizeDouble(price, digits);
      if(vol <= 0.0 || price <= 0.0 || ask <= 0.0 || bid <= 0.0)
        {
         AppendOut(id, "ERR", "bad volume or no quote");
         return;
        }
      double stops  = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * point;
      double freeze = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL) * point;
      double gap    = MathMax(stops, freeze);
      string why = "";
      if(ptype == ORDER_TYPE_BUY_LIMIT  && price > ask - gap) why = "buy limit must sit BELOW the ask";
      if(ptype == ORDER_TYPE_BUY_STOP   && price < ask + gap) why = "buy stop must sit ABOVE the ask";
      if(ptype == ORDER_TYPE_SELL_LIMIT && price < bid + gap) why = "sell limit must sit ABOVE the bid";
      if(ptype == ORDER_TYPE_SELL_STOP  && price > bid - gap) why = "sell stop must sit BELOW the bid";
      if(why != "")
        {
         AppendOut(id, "ERR", why + " (bid " + DoubleToString(bid, digits) +
                   " ask " + DoubleToString(ask, digits) + ")");
         return;
        }
      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action       = TRADE_ACTION_PENDING;
      req.symbol       = sym;
      req.volume       = NormalizeVolume(sym, vol);
      req.type         = (ENUM_ORDER_TYPE)ptype;
      req.price        = price;
      req.sl           = (sl > 0.0) ? NormalizeDouble(sl, digits) : 0.0;
      req.tp           = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
      req.magic        = (ulong)mg;
      req.comment      = cm;
      req.type_time    = ORDER_TIME_GTC;
      req.type_filling = ORDER_FILLING_RETURN;
      if(!OrderSend(req, res) ||
         (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_PLACED))
        {
         AppendOut(id, "ERR", "retcode " + IntegerToString((long)res.retcode) +
                   " " + res.comment);
         return;
        }
      AppendOut(id, "OK", DoubleToString(req.price, digits) + "|" +
                IntegerToString((long)res.order) + "|" +
                DoubleToString(req.volume, 2));
      return;
     }

   if(cmd == "CLOSE" && k >= 3)
     {
      ulong ticket = (ulong)StringToInteger(p[2]);
      if(!PositionSelectByTicket(ticket))
        {
         // Not an open position - it may be a pending stop/limit order
         // (StopExe straddle, manual ORDER ticket).  Delete it instead so
         // the panel's CLOSE buttons flatten pending orders too.
         if(OrderSelect(ticket))
           {
            string osym = OrderGetString(ORDER_SYMBOL);
            MqlTradeRequest delreq;
            MqlTradeResult  delres;
            ZeroMemory(delreq);
            ZeroMemory(delres);
            delreq.action = TRADE_ACTION_REMOVE;
            delreq.order  = ticket;
            if(!OrderSend(delreq, delres) ||
               (delres.retcode != TRADE_RETCODE_DONE &&
                delres.retcode != TRADE_RETCODE_PLACED))
              {
               AppendOut(id, "ERR", "cancel retcode " +
                         IntegerToString((long)delres.retcode) + " " + delres.comment);
               return;
              }
            DumpOrders();
            AppendOut(id, "OK", "deleted pending " + osym + "|" + p[2] + "|0.00");
            return;
           }
         AppendOut(id, "ERR", "no position or pending order " + p[2]);
         return;
        }
      string sym  = PositionGetString(POSITION_SYMBOL);
      long   type = PositionGetInteger(POSITION_TYPE);
      double vol  = PositionGetDouble(POSITION_VOLUME);
      int digits  = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double price = (type == POSITION_TYPE_BUY)
                      ? SymbolInfoDouble(sym, SYMBOL_BID)
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
      ENUM_ORDER_TYPE_FILLING f = ProvenFill(sym);
      if(f == 0) f = FillingFor(sym);
      req.type_filling = f;
      MqlTradeResult ares;
      ZeroMemory(ares);
      if(!OrderSendAsync(req, ares) || ares.retcode == 0 ||
         (ares.retcode != TRADE_RETCODE_PLACED &&
          ares.retcode != TRADE_RETCODE_DONE &&
          ares.retcode != TRADE_RETCODE_DONE_PARTIAL))
        {
         AppendOut(id, "ERR", "retcode " + IntegerToString((long)ares.retcode));
         return;
        }
      AsyncTrack(ares.request_id, id, false, sym, vol, digits, (long)f, req);
      return;
     }

   if(cmd == "CLOSEALL" && k >= 3)
     {

      string sym = p[2];
      string symup = sym;
      StringToUpper(symup);
      bool all = (symup == "ALL");

      int total = PositionsTotal();
      if(total > 0)
        {
         // Async fan-out: every close is flighted in parallel, so a basket
         // closes in ~one fill time, not N sequential fills.  Python does
         // NOT trust the first ack - it polls the position book until empty
         // (fast) and retries any remainder, so speed costs no truth.
         MqlTradeRequest reqs[];
         ArrayResize(reqs, total);
         int nreq = 0;
         for(int i = total - 1; i >= 0; i--)
           {
            ulong ticket = PositionGetTicket(i);
            if(ticket == 0 || !PositionSelectByTicket(ticket))
               continue;
            string psym = PositionGetString(POSITION_SYMBOL);
            if(!all && psym != sym)
               continue;
            long   type  = PositionGetInteger(POSITION_TYPE);
            double vol   = PositionGetDouble(POSITION_VOLUME);
            int    dg    = (int)SymbolInfoInteger(psym, SYMBOL_DIGITS);
            double price = (type == POSITION_TYPE_BUY)
                            ? SymbolInfoDouble(psym, SYMBOL_BID)
                            : SymbolInfoDouble(psym, SYMBOL_ASK);
            MqlTradeRequest req;
            ZeroMemory(req);
            req.action       = TRADE_ACTION_DEAL;
            req.symbol       = psym;
            req.position     = ticket;
            req.volume       = vol;
            req.type         = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
            req.price        = NormalizeDouble(price, dg);
            req.deviation    = 20;
            ENUM_ORDER_TYPE_FILLING f = ProvenFill(psym);
            if(f == 0) f = FillingFor(psym);
            req.type_filling = f;
            reqs[nreq++] = req;
           }
         int sent = 0, failed = 0;
         for(int i = 0; i < nreq; i++)
           {
            MqlTradeResult ares;
            ZeroMemory(ares);
            if(OrderSendAsync(reqs[i], ares) &&
               (ares.retcode == TRADE_RETCODE_PLACED ||
                ares.retcode == TRADE_RETCODE_DONE ||
                ares.retcode == TRADE_RETCODE_DONE_PARTIAL))
              {
               AsyncTrack(ares.request_id, id, false, reqs[i].symbol, reqs[i].volume,
                          (int)SymbolInfoInteger(reqs[i].symbol, SYMBOL_DIGITS),
                          (long)reqs[i].type_filling, reqs[i]);
               sent++;
              }
            else
               failed++;
           }
         if(sent == 0 && nreq == 0)
           {
            // No matching positions - fall through to the pending-order
            // sweep below instead of erroring, so CLOSEALL on a book that
            // holds only stops still deletes them.
           }
         else if(sent == 0)
           {
            AppendOut(id, "ERR", "no close sent (failed " +
                              IntegerToString(failed) + ")");
            return;
           }
         else
           {
            // Positions are flighted async (tracked) - pendings delete
            // synchronously right now, then one combined ack goes back.
            int deld = 0, delfail = 0;
            for(int oi = OrdersTotal() - 1; oi >= 0; oi--)
              {
               ulong ot = OrderGetTicket(oi);
               if(ot == 0 || !OrderSelect(ot))
                  continue;
               if(!all && OrderGetString(ORDER_SYMBOL) != sym)
                  continue;
               MqlTradeRequest dreq;
               MqlTradeResult  dres;
               ZeroMemory(dreq);
               ZeroMemory(dres);
               dreq.action = TRADE_ACTION_REMOVE;
               dreq.order  = ot;
               if(OrderSend(dreq, dres) &&
                  (dres.retcode == TRADE_RETCODE_DONE ||
                   dres.retcode == TRADE_RETCODE_PLACED))
                  deld++;
               else
                  delfail++;
              }
            DumpOrders();
            AppendOut(id, "OK", "closed flighted " + IntegerToString(sent) +
                      " deleted pendings " + IntegerToString(deld) +
                      (delfail > 0 ? (" failed " + IntegerToString(failed + delfail)) : ""));
            return;
           }
        }
      // No positions at all - still sweep pending stop/limit orders so the
      // red CLOSE bar deletes a StopExe straddle sitting on the broker.
      {
       int deld = 0, delfail = 0;
       for(int oi = OrdersTotal() - 1; oi >= 0; oi--)
         {
          ulong ot = OrderGetTicket(oi);
          if(ot == 0 || !OrderSelect(ot))
             continue;
          if(!all && OrderGetString(ORDER_SYMBOL) != sym)
             continue;
          MqlTradeRequest dreq;
          MqlTradeResult  dres;
          ZeroMemory(dreq);
          ZeroMemory(dres);
          dreq.action = TRADE_ACTION_REMOVE;
          dreq.order  = ot;
          if(OrderSend(dreq, dres) &&
             (dres.retcode == TRADE_RETCODE_DONE ||
              dres.retcode == TRADE_RETCODE_PLACED))
             deld++;
          else
             delfail++;
         }
       DumpOrders();
       if(deld == 0 && delfail == 0)
         {
          AppendOut(id, "OK", "closed 0 failed 0");
          return;
         }
       if(deld > 0 && delfail == 0)
         {
          AppendOut(id, "OK", "closed 0 deleted pendings " + IntegerToString(deld));
          return;
         }
       AppendOut(id, "ERR", "deleted " + IntegerToString(deld) +
                 " failed " + IntegerToString(delfail));
       return;
      }
     }

   if(cmd == "PROBE" && k >= 3)
     {
      string sym = p[2];
      if(!SymbolSelect(sym, true))
        {
         AppendOut(id, "ERR", "unknown symbol " + sym);
         return;
        }

      AppendOut(id, "OK",
                sym + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_DIGITS)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_FILLING_MODE)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_EXEMODE)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_MODE)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_ORDER_MODE)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL)) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL)) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), 2) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX), 2) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP), 2) + "|" +
                IntegerToString(SymbolInfoInteger(sym, SYMBOL_SPREAD)) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE), 2) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE), 8) + "|" +
                DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE), 8));
      return;
     }

   AppendOut(id, "ERR", "unknown cmd " + cmd);
  }

