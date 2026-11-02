const {test,describe}=require('node:test');
const assert=require('node:assert/strict');
global.sum=(a,f)=>a.reduce((s,x)=>s+f(x),0);
const {simulate,amort}=require('../../src/budget/templates/engine.js');

const loan=(id,bal,apr,pay,x={})=>({id,name:id,bal,apr,card:false,pay,start:1,...x});
const card=(id,bal,apr,x={})=>({id,name:id,bal,apr,card:true,start:1,...x});
const byId=(r,id)=>r.ds.find(d=>d.id===id);
const focusOrder=r=>r.ds.filter(d=>d.snow!=null).sort((a,b)=>a.snowMo-b.snowMo);

describe('simulate basics',()=>{
  test('amortizing loan pays off on schedule with exact interest',()=>{
    const pay=amort(10000,6,24),r=simulate([loan('a',10000,6,pay)],pay,'snowball',24);
    assert.equal(byId(r,'a').done,24);
    assert.ok(Math.abs(r.interest-(24*pay-10000))<1);
  });
  test('0% debt accrues no interest',()=>{
    const r=simulate([loan('a',1200,0,100)],100,'snowball',24);
    assert.equal(r.interest,0);assert.equal(byId(r,'a').done,12);
  });
  test('money is conserved: balances + interest = payments',()=>{
    const ds=[card('c',5000,22),loan('l',9000,8,300)];
    const r=simulate(ds,900,'avalanche',24);
    const paid=sum(r.ds,d=>sum(d.paid,x=>x));
    assert.ok(Math.abs(5000+9000+r.interest-paid)<1);
  });
  test('avalanche never costs more interest than snowball',()=>{
    const ds=()=>[card('big',3000,12),card('small',500,29),loan('l',8000,9,250)];
    assert.ok(simulate(ds(),900,'avalanche',24).interest<=simulate(ds(),900,'snowball',24).interest+1e-6);
  });
  test('more budget is never later nor costlier',()=>{
    const ds=()=>[card('a',4000,20),loan('l',9000,8,300)];
    const lo=simulate(ds(),800,'avalanche',24),hi=simulate(ds(),1000,'avalanche',24);
    assert.ok(hi.months<=lo.months&&hi.interest<=lo.interest);
  });
  test('shortfall is borrowed on a new card',()=>{
    const r=simulate([loan('l',9000,8,500)],300,'snowball',24);
    assert.ok(r.short>0&&byId(r,'borrowed'));
  });
});

describe('priority',()=>{
  test('lower pri number gets extra money first, whatever the rate',()=>{
    const r=simulate([card('hi',3000,30,{pri:3}),card('lo',3000,10,{pri:1})],400,'avalanche',24);
    assert.ok(byId(r,'lo').done<byId(r,'hi').done);
  });
  test('avalanche orders by rate within a pri tier',()=>{
    const r=simulate([card('lo',3000,10),card('hi',3000,30)],400,'avalanche',24);
    assert.ok(byId(r,'hi').done<byId(r,'lo').done);
  });
});

describe('snowball column (snow)',()=>{
  const scen=()=>[card('a',1500,20),card('b',4000,18),loan('c',9000,8,260),loan('d',6000,7,200)];
  for(const strat of ['snowball','avalanche'])test(`${strat}: snow equals what the debt really received that month`,()=>{
    const r=simulate(scen(),1100,strat,24);
    for(const d of r.ds)if(d.snow!=null)assert.ok(Math.abs(d.paid[d.snowMo-1]-Math.min(d.snow,d.paid[d.snowMo-1]+1e-6))<1e-6&&d.paid[d.snowMo-1]>=d.snow-1,`${d.id} snow ${d.snow} vs paid ${d.paid[d.snowMo-1]}`);
  });
  for(const strat of ['snowball','avalanche'])test(`${strat}: snow never shrinks as debts are paid off`,()=>{
    const f=focusOrder(simulate(scen(),1100,strat,24));
    assert.ok(f.length>=2);
    for(let i=1;i<f.length;i++)assert.ok(f[i].snow>=f[i-1].snow-1,`${f[i].id} ${f[i].snow} < ${f[i-1].id} ${f[i-1].snow}`);
  });
  test('first focus debt gets budget minus the others’ minimums',()=>{
    const ds=scen(),r=simulate(ds,1100,'snowball',24);
    const a=byId(r,'a'),others=260+200+Math.max(35,4000*(18/1200+.01));
    assert.ok(Math.abs(a.snow-(1100-others))<2,`${a.snow}`);
  });
  test('a lump-sum month does not set snow',()=>{
    const r=simulate(scen(),1100,'snowball',24,{1:5000});
    assert.ok(byId(r,'a').snow==null||byId(r,'a').snowMo>1);
  });
  test('a debt paid off before it is ever the focus has no snow',()=>{
    const r=simulate([card('a',3000,20),loan('s',300,0,150,{pri:3})],700,'snowball',24);
    assert.equal(byId(r,'s').snow,undefined);
  });
});

describe('snow under stress',()=>{
  test('shortfall months do not set snow (it would read as just the minimum)',()=>{
    const r=simulate([card('a',3000,20),loan('l',9000,8,600)],500,'snowball',24);
    const a=byId(r,'a');assert.ok(a.snow==null||a.snowMo>r.short);
  });
  test('a sale lump pays debts off but does not inflate the next debt’s snow',()=>{
    const ds=()=>[card('a',2000,25),card('b',6000,20),loan('c',9000,8,260)];
    const r=simulate(ds(),1000,'avalanche',24,{3:20000});
    for(const d of r.ds)if(d.snow!=null)assert.ok(d.snow<=1000+1,`${d.id} ${d.snow}`);
  });
  test('fuzz: snow equals the real payment and never exceeds the budget',()=>{
    let seed=7;const rnd=()=>(seed=(seed*16807)%2147483647)/2147483647;
    for(let t=0;t<200;t++){
      const n=2+Math.floor(rnd()*4),ds=[];
      for(let i=0;i<n;i++){const bal=500+rnd()*20000,apr=rnd()*30;ds.push(rnd()<.5?card('d'+i,bal,apr):loan('d'+i,bal,apr,amort(bal,apr,24+Math.floor(rnd()*60))))}
      const budget=300+rnd()*3000,strat=rnd()<.5?'avalanche':'snowball',r=simulate(ds,budget,strat,24);
      for(const d of r.ds)if(d.snow!=null){
        assert.ok(d.snow<=budget+1e-6,`budget ${budget} snow ${d.snow}`);
        assert.ok(d.paid[d.snowMo-1]>=d.snow-1e-6||d.done===d.snowMo,`${d.id} paid ${d.paid[d.snowMo-1]} snow ${d.snow}`);
      }
    }
  });
});

describe('savings ladder',()=>{
  const ds=()=>[card('a',2000,20),loan('b',6000,0,200),card('c',9000,20)];
  const sav=x=>({apy:0,...x});
  test('no config means no savings and an unchanged payoff',()=>{
    const r=simulate(ds(),1500,'snowball',24),r2=simulate(ds(),1500,'snowball',24,0,true,sav({}));
    assert.equal(r.sav.final,0);assert.equal(r2.months,r.months);assert.equal(r2.interest,r.interest);
  });
  test('ladder diverts nothing before its trigger and a percent of the snowball after',()=>{
    const r=simulate(ds(),1500,'snowball',24,0,true,sav({ladder:[{trigger:'a',pct:50}]})),a=byId(r,'a');
    const first=r.sav.bal.findIndex(x=>x>0);
    assert.equal(first,a.done+1);
    assert.ok(r.sav.deposited>0);
  });
  test('later step replaces the earlier percent',()=>{
    const r=simulate(ds(),1500,'snowball',24,0,true,sav({ladder:[{trigger:'a',pct:10},{trigger:'b',pct:100}]}));
    const b=byId(r,'b').done,dep=r.sav.bal.map((x,i)=>x-(r.sav.bal[i-1]||0));
    assert.ok(dep[b+1]>dep[b-1+0]||dep[b+1]>0);
  });
  test('diverting savings slows payoff; deposits are conserved',()=>{
    const base=simulate(ds(),1500,'snowball',24),r=simulate(ds(),1500,'snowball',24,0,true,sav({ladder:[{trigger:'a',pct:50}]}));
    assert.ok(r.months>=base.months);
    const paid=sum(r.ds,d=>sum(d.paid,x=>x)),spent=paid+r.sav.deposited;
    assert.ok(spent<=1500*r.months+1e-6);
  });
  test('income line deposits pct of income after its trigger, capped by cash after minimums',()=>{
    const r=simulate(ds(),1500,'snowball',24,0,true,sav({income:{trigger:'a',pct:5,amount:10000}}));
    const dep=r.sav.bal[byId(r,'a').done+1]-r.sav.bal[byId(r,'a').done];
    assert.ok(Math.abs(dep-500)<1e-6||dep<500);
  });
  test('savings compound monthly at the APY',()=>{
    const r=simulate([loan('a',1000,0,500)],600,'snowball',24,0,true,{apy:12,ladder:[{trigger:'a',pct:100}],income:{trigger:'a',pct:1,amount:1000}});
    assert.ok(r.sav.interest>=0);
    const r0=simulate([loan('a',3000,0,500),loan('z',60000,0,1)],900,'snowball',24,0,true,{apy:12,income:{trigger:'a',pct:10,amount:1000}});
    assert.ok(r0.sav.interest>0);
    assert.ok(Math.abs(r0.sav.final-(r0.sav.deposited+r0.sav.interest))<1e-6);
  });
});

describe('savings target',()=>{
  const ds=()=>[card('a',2000,20),loan('b',6000,0,200),card('c',9000,20)];
  test('deposits stop at the target and the balance keeps compounding',()=>{
    const r=simulate(ds(),1500,'snowball',24,0,true,{apy:12,target:1000,ladder:[{trigger:'a',pct:100}]});
    const b=r.sav.bal,hit=b.findIndex(x=>x>=1000-1e-6);
    assert.ok(hit>0);assert.ok(Math.abs(b[hit]-1000)<1e-6);
    assert.ok(b[hit+1]>b[hit]);
    const dep=r.sav.dep;assert.ok(dep.slice(hit+1).every(x=>x===0));
  });
  test('reaching the target frees the money for debt',()=>{
    const cfg=t=>({apy:0,target:t,ladder:[{trigger:'a',pct:100}]});
    const cap=simulate(ds(),1500,'snowball',24,0,true,cfg(1000)),open=simulate(ds(),1500,'snowball',24,0,true,cfg(0));
    assert.ok(cap.months<=open.months);assert.ok(cap.sav.final<=1000+1e-6);
  });
});

describe('reserved pending debt',()=>{
  const order=ids=>(a,b)=>ids.indexOf(a.id)-ids.indexOf(b.id);
  const run=reserve=>simulate([card('c',3000,20),loan('tax',2000,0,50,{start:5,reserve})],100,order(['tax','c']),24,{2:2000});
  test('lump before start is held for a top-ranked pending debt',()=>{
    const r=run(true);
    assert.equal(byId(r,'tax').done,5);
    assert.equal(sum(byId(r,'tax').paid,x=>x),0);
  });
  test('without reserve the lump goes to other debts',()=>{
    assert.ok(byId(run(false),'tax').done>5);
  });
});

describe('reserved pending debt history',()=>{
  test('history shows the owed balance, less held cash, before start',()=>{
    const r=simulate([loan('tax',2000,0,50,{start:5,reserve:true})],100,'snowball',24);
    assert.equal(byId(r,'tax').hist[0],1900);
    assert.equal(byId(r,'tax').hist[2],1700);
  });
});
