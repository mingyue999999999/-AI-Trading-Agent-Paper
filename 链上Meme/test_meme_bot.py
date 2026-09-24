import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import 链上Meme机器人 as b

class Tests(unittest.TestCase):
    def pair(self,**kw):
        p={"chainId":"solana","priceUsd":"1","liquidity":{"usd":500000},"fdv":5000000,
           "volume":{"h24":1000000},"txns":{"h24":{"buys":700,"sells":600}},
           "priceChange":{"h1":5,"h24":20},"pairCreatedAt":int((b.time.time()-86400)*1000)}
        p.update(kw); return p
    def test_good_pair_passes(self): self.assertIsNotNone(b.score_pair(self.pair()))
    def test_thin_liquidity_rejected(self): self.assertIsNone(b.score_pair(self.pair(liquidity={"usd":1000})))
    def test_corrupt_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.json"; p.write_text("bad")
            with self.assertRaises(RuntimeError): b.load_account(p)
    def test_initial_capital(self): self.assertEqual(b.new_account()["cash"],10000.0)

if __name__=="__main__": unittest.main()
