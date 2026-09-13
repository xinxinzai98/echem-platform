const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {document:{activeElement:null,addEventListener(){}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../static/start-stop-plot-interaction.js'),'utf8'),context);
const handlers = {};
const frame = {dataset:{}, addEventListener:(name,fn)=>{handlers[name]=fn;},
  getBoundingClientRect:()=>({left:0,top:0,width:200,height:100}),
  focus:()=>{context.document.activeElement=frame;}, setPointerCapture(){},hasPointerCapture:()=>true,releasePointerCapture(){}};
const selection={hidden:true,style:{}};
const menu={hidden:true,style:{},contains:()=>false,querySelector:()=>({addEventListener(){},focus(){}})};
const base={xMin:0,xMax:10,yMin:0,yMax:20};
let geometry={width:200,height:100,plot:{left:20,top:10,width:160,height:80},domain:{...base}};
let inspected=0;
const interaction=new context.StartStopPlotInteraction({frame,selection,menu,
  geometry:()=>geometry, domain:value=>{geometry={...geometry,domain:{...value}};},
  reset:()=>{geometry={...geometry,domain:{...base}};}, inspect:()=>{inspected++;}});
const event=(x,y)=>({clientX:x,clientY:y,pointerId:1,button:0,target:frame,preventDefault(){}});
interaction.zoom(0.5);
assert.deepEqual(geometry.domain,{xMin:2.5,xMax:7.5,yMin:5,yMax:15});
interaction.reset();
interaction.setMode('zoom');
interaction.begin(event(40,20)); interaction.move(event(160,80));
assert.equal(selection.hidden,false);
interaction.finish(event(160,80));
assert.deepEqual(geometry.domain,{xMin:1.25,xMax:8.75,yMin:2.5,yMax:17.5});
assert.equal(selection.hidden,true);
interaction.reset();
interaction.setMode('pan');
interaction.begin(event(100,50)); interaction.move(event(116,58));
assert.deepEqual(geometry.domain,{xMin:-1,xMax:9,yMin:2,yMax:22});
interaction.cancel();
assert.deepEqual(geometry.domain,base);
interaction.setMode('inspect');
interaction.begin(event(-1,-1)); assert.equal(interaction.drag,null);
interaction.begin(event(100,50)); interaction.finish(event(101,51));
assert.equal(inspected,1);
handlers.contextmenu(event(100,50)); assert.equal(menu.hidden,false);
handlers.keydown({key:'Escape',preventDefault(){}}); assert.equal(menu.hidden,true);
console.log('PASS: zoom geometry, rectangular zoom, pan cancellation, inspection and context menu');
