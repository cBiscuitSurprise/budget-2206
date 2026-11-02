const addM=(ym,n)=>{let[y,m]=ym.split('-').map(Number);m+=n-1;y+=Math.floor(m/12);m=(m%12+12)%12+1;return`${y}-${String(m).padStart(2,'0')}`};
const amort=(b,apr,n)=>{const r=apr/1200;return r?b*r/(1-(1+r)**-n):b/n};

function simulate(debts,budget,strategy,cardApr,lump=0,pri=true,sav=null){
  if(pri&&debts.some(d=>d.sellAt==='gap')){
    const cut=Math.min(...debts.filter(d=>d.sellAt==='gap').map(d=>d.sellBy??0));
    const pre=cut?simulate(debts,budget,strategy,cardApr,lump,false,sav).ds:[],early=new Set(pre.filter(d=>d.done!=null&&d.done<=cut).map(d=>d.id));
    debts=debts.map(d=>({...d,pri:d.sellAt==='gap'?1:early.has(d.id)?0:d.pri??2}));
  }
  const ds=debts.map(d=>({...d,full:d.bal,held:0,bal:d.start>1?0:d.bal,interest:0,done:null,hist:[],paid:[],pm:0}));
  const pending=mo=>ds.some(d=>d.start>mo);
  const main=()=>sum(ds,d=>d.follow?0:Math.max(0,d.bal)),fam=()=>sum(ds,d=>d.follow?Math.max(0,d.bal):0);
  const total=[main()],ftotal=[fam()];let interest=0,short=0,gaps=[],borrowed=null,mo=0,months=null,fmonths=null,amonths=null,carry=0;
  const savings=[0],deps=[0];let sbal=0,sdep=0,sint=0;
  const doneId=id=>ds.some(d=>d.id===id&&d.done!=null),rate=sav?(sav.apy||0)/1200:0,target=sav?.target>0?sav.target:Infinity;
  const room=d=>d.reserve&&d.start>mo?d.full-d.held:d.bal-(d.sellAt!=null?d.value||0:0);
  while((ds.some(d=>d.bal>.005)||pending(mo))&&mo<600){
    mo++;
    for(const d of ds)d.pm=0;
    for(const d of ds)if(d.start===mo){d.bal=Math.max(0,d.full-d.held);d.held=0}
    for(const d of ds)if(d.sellAt!=null&&d.dep&&mo>1)d.value*=(1-d.dep/100)**(1/12);
    const mins=[];
    for(const d of ds){if(d.bal<=.005)continue;const i=d.bal*d.apr/1200;d.bal+=i;d.interest+=i;interest+=i;mins.push([d,Math.min(d.bal,d.card?Math.max(35,i+.01*(d.bal-i)):d.pay||0)])}
    const own=ids=>{const g=ds.filter(d=>ids.includes(d.id));return g.length?g.filter(d=>d.sold==null&&!d.gone).length/g.length:1};
    const carried=carry,lumpNow=typeof lump==='number'?(mo===1?lump:0):(lump[mo]||0)+sum((lump.scaled||[]).filter(x=>x.mo===mo),x=>x.amount*own(x.ids));let cash=budget+carry+lumpNow;carry=0;const need=sum(mins,x=>x[1]);
    let dep=0;const space=()=>Math.max(0,target-sbal*(1+rate)-dep);
    if(sav?.income&&doneId(sav.income.trigger))dep=Math.min(sav.income.pct/100*sav.income.amount,Math.max(0,cash-need),space());
    cash-=dep;
    if(need>cash){
      short++;gaps.push([mo,need-cash]);
      if(!borrowed){borrowed={id:'borrowed',name:'New card debt (budget short of minimums)',bal:0,apr:cardApr,card:true,interest:0,done:null,hist:[],paid:[],pm:0};ds.push(borrowed)}
      borrowed.bal+=need-cash;cash=need;
    }
    for(const[d,m]of mins){d.bal-=m;d.pm+=m;cash-=m}
    const step=sav?.ladder?.filter(x=>doneId(x.trigger)).pop();
    if(step&&cash>.005){const x=Math.min(cash,Math.max(0,cash-lumpNow-carried)*step.pct/100,space());dep+=x;cash-=x}
    const by=typeof strategy==='function'?strategy:strategy==='avalanche'?(a,b)=>b.apr-a.apr||room(a)-room(b):(a,b)=>room(a)-room(b);
    const tier=d=>d.last?1e3:d.follow||0,order=(a,b)=>tier(a)-tier(b)||(a.pri??2)-(b.pri??2)||by(a,b);
    const top=ds.filter(d=>room(d)>.005&&!(d.reserve&&d.start>mo)).sort(order)[0];
    if(top&&top.snow==null&&cash>.005&&!lumpNow&&!carried){const own=mins.find(x=>x[0]===top)?.[1]||0;top.snow=own+cash;top.snowMo=mo}
    while(cash>.005){const t=ds.filter(d=>room(d)>.005).sort(order)[0];if(!t)break;const p=Math.min(cash,room(t));if(t.reserve&&t.start>mo)t.held+=p;else{t.bal-=p;t.pm+=p}cash-=p}
    for(const d of ds){
      if(d.sellAt==null||d.bal<=.005||!(d.sellAt==='gap'?room(d)<=.005||mo>=(d.sellBy||1e9):mo>=d.sellAt))continue;
      d.lost=(debts.find(x=>x.id===d.id).value||0)-d.value;
      carry+=Math.max(0,d.value-d.bal);d.bal=Math.max(0,d.bal-d.value);d.sold=mo;d.sellAt=null;d.value=0;
      if(d.bal>.005){d.apr=d.gapApr;d.pay=amort(d.bal,d.gapApr,36)}
    }
    for(const d of ds)if(d.bal<=.005&&d.done==null&&!(d.start>mo)){d.bal=0;d.done=mo}
    sint+=sbal*rate;sbal+=sbal*rate+dep;sdep+=dep;savings.push(sbal);deps.push(dep);
    for(const d of ds){d.hist.push(d.reserve&&d.start>mo?d.full-d.held:d.bal);d.paid.push(d.pm)}
    total.push(main());ftotal.push(fam());
    if(months==null&&!ds.some(d=>!d.follow&&!d.last&&(d.bal>.005||d.start>mo)))months=mo;
    if(fmonths==null&&months!=null&&!ds.some(d=>d.follow&&d.bal>.005))fmonths=mo;
    if(amonths==null&&!ds.some(d=>!d.follow&&(d.bal>.005||d.start>mo)))amonths=mo;
  }
  if(months==null&&!ds.some(d=>!d.follow&&!d.last&&d.bal>.005))months=0;
  if(fmonths==null&&months!=null&&!ds.some(d=>d.follow&&d.bal>.005))fmonths=months;
  if(amonths==null&&!ds.some(d=>!d.follow&&d.bal>.005))amonths=0;
  return{ds,total:amonths==null?total:total.slice(0,amonths+1),ftotal,interest,lost:sum(ds,d=>d.lost||0),short,gaps,months,fmonths,sav:{bal:amonths==null?savings:savings.slice(0,amonths+1),dep:deps,deposited:sdep,interest:sint,final:sbal}};
}
if(typeof module!=='undefined')module.exports={addM,amort,simulate,sum};
