import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import new_launch_shadow as b

class Tests(unittest.TestCase):
    def metrics(self,**kw):
        x={"price":1,"age_h":1,"liquidity":100000,"volume24":250000,"fdv":1000000,
           "liq_fdv":.10,"buys":250,"sells":150,"h1":10,"h24":20}
        x.update(kw); return x
    def candidate(self):
        m=self.metrics()
        return {"chain":"solana","address":"abc","symbol":"NEW","score":b.score_launch(m),"metrics":m}
    def test_screen_and_initial_capital(self):
        self.assertIsNotNone(b.score_launch(self.metrics()))
        self.assertIsNone(b.score_launch(self.metrics(liquidity=1000)))
        self.assertEqual(b.new_account()["cash"],10000.0)
    def test_observation_gate_and_cap(self):
        a=b.new_account(); c=self.candidate(); start=1_000_000
        for i in range(3): b.run(a,[c],{},start+i*900)
        self.assertFalse(a["positions"])
        b.run(a,[c],{},start+2700)
        self.assertEqual(len(a["positions"]),1)
        self.assertLessEqual(10000-a["cash"],100.01)
    def test_staged_profit_and_trailing_exit(self):
        a=b.new_account(); c=self.candidate(); start=1_000_000
        for i in range(4): b.run(a,[c],{},start+i*900)
        key="solana:abc"
        b.run(a,[c],{key:1.31},start+3600)
        self.assertEqual(a["positions"][key]["profit_stage"],1)
        b.run(a,[c],{key:1.61},start+4500)
        self.assertEqual(a["positions"][key]["profit_stage"],2)
        b.run(a,[c],{key:1.40},start+5400)
        self.assertNotIn(key,a["positions"])
    def test_corrupt_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"a.json"; p.write_text("bad")
            with self.assertRaises(RuntimeError): b.load_account(p)

if __name__=="__main__": unittest.main()
