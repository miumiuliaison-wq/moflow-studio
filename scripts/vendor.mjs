import fs from 'node:fs';import path from 'node:path';
for(const id of ['arc-foundation','arc-motion-tokens','activity-heatmap','sortable-data-table']){
 const item=JSON.parse(fs.readFileSync(`vendor/${id}.json`,'utf8'));
 for(const f of item.files){const dest='src/components/arc/'+f.path.replace(/^registry\//,'');fs.mkdirSync(path.dirname(dest),{recursive:true});fs.writeFileSync(dest,f.content.replaceAll('"../lib/motion-tokens"','"../../motion-tokens"'));}
}
