<script setup lang="ts">
import {computed,ref,watch} from 'vue';
import {num,percent} from '../utils/format';
import * as diagnosesApi from '../api/diagnoses';
import type {DiagnosisSummary,LooBlockHeader,LooBlock,LooBlockFull,LooPairFull,LooPairRecord} from '../types/domain';
const props=defineProps<{
  diagnoses:DiagnosisSummary[];
  current:DiagnosisSummary|null;
  busy:boolean;
  canEdit:boolean;
}>();
const emit=defineEmits<{(e:'create'):void;(e:'open',id:number):void}>();
const excludedCode=ref('');
const slimBlock=ref<LooBlock|null>(null);
const evidenceBlock=ref<LooBlockFull|null>(null);
const detailLoading=ref(false);
const evidenceLoading=ref(false);
const detailError=ref('');
const evidenceError=ref('');
const relationLabel:Record<string,string>={greater:'严格大于',less:'严格小于',tie:'计算值并列',unchanged:'无变化',strict_inversion:'严格逆序',tie_change:'并列变化'};
const completedDetail=computed(()=>props.current&&props.current.status==='completed'?props.current as unknown as {result:import('../types/domain').LooResult}:null);
const headers=computed<LooBlockHeader[]>(()=>completedDetail.value?.result.blocks??[]);
const selectedHeader=computed(()=>headers.value.find(b=>b.excluded_code===excludedCode.value)??null);
const changedPairs=computed<LooPairRecord[]>(()=>(slimBlock.value?.pairs??[]).filter(p=>p.strict_inversion_count||p.tie_change_count||p.center_change!=='unchanged'));
function pctOf(d:DiagnosisSummary){const p=d.progress;return p&&p.total?Math.round(p.done/p.total*100):0;}
const criteria=computed(()=>completedDetail.value?.result.criteria_codes??[]);
const topPairs=computed(()=>completedDetail.value?.result.aggregate.pair_summary.slice(0,12)??[]);
const evidencePairs=computed<LooPairFull[]>(()=>evidenceBlock.value?.pairs??[]);
async function loadBlockDetail(code:string){
  if(!props.current)return;
  detailLoading.value=true;detailError.value='';
  try{slimBlock.value=await diagnosesApi.block(props.current.id,code);}
  catch(e){detailError.value=(e as Error).message;slimBlock.value=null;}
  finally{detailLoading.value=false;}
}
function pickBlock(code:string){excludedCode.value=code;evidenceBlock.value=null;evidenceError.value='';loadBlockDetail(code);}
async function loadEvidence(){
  if(!props.current||!excludedCode.value)return;
  evidenceLoading.value=true;evidenceError.value='';
  try{
    const doc=await diagnosesApi.evidence(props.current.id,excludedCode.value);
    evidenceBlock.value=doc.blocks[0]??null;
  }catch(e){evidenceError.value=(e as Error).message;}finally{evidenceLoading.value=false;}
}
watch(headers,list=>{
  if(list.length&&!list.some(b=>b.excluded_code===excludedCode.value))pickBlock(list[0].excluded_code);
},{immediate:true});
</script>
<template>
  <section class="loo-panel">
    <div class="section-line">
      <h2 class="ruled-title" style="border:0;padding:0;margin:0">候选剔除诊断 <small>逐一剔除，看谁在改变排序</small></h2>
      <button class="primary" :disabled="busy||!canEdit" @click="emit('create')">{{busy?'提交中…':'对当前权衡发起剔除诊断 →'}}</button>
    </div>
    <p class="muted">沿用该权衡已冻结的测量与同一批权重情景（不重新抽样）。每次剔除一份候选后重新做 L2 标准化与 TOPSIS：列范数、正负理想点随之改变，剩余试样可能互换名次。严格逆序与并列变化分开计数；原权衡结果只读，不会被改写。</p>

    <div v-if="!diagnoses.length" class="empty"><h3>尚无候选诊断。</h3><p>对一份已完成的权衡发起诊断，worker 会逐份剔除候选并在每个候选处存档，可中断续跑。</p></div>

    <div v-if="diagnoses.length" class="loo-list">
      <article v-for="d in diagnoses" :key="d.id" :class="['run-entry',{selected:current?.id===d.id}]">
        <div class="entry-line">
          <small>DIAGNOSIS #{{String(d.id).padStart(3,'0')}} · RUN #{{d.run_id}}</small>
          <span :class="['status',d.status]">{{({queued:'已排队',running:'诊断演算中',completed:'已完成',failed:'未能计算'} as Record<string,string>)[d.status]}}</span>
        </div>
        <h3>{{d.name}}</h3>
        <p v-if="d.progress?.total">{{d.progress.done}}/{{d.progress.total}} 份候选已剔除存档<span v-if="d.progress.last_excluded"> · 最近：{{d.progress.last_excluded}}</span></p>
        <div v-if="['queued','running'].includes(d.status)" class="loo-progress" role="progressbar" :aria-valuenow="pctOf(d)" aria-valuemin="0" aria-valuemax="100">
          <i :style="{width:pctOf(d)+'%'}"/><span>{{pctOf(d)}}%</span>
        </div>
        <p v-if="d.error" class="error-text">{{d.error}}</p>
        <div v-if="d.status==='completed'&&typeof d.summary.total_strict_inversion_events==='number'" class="loo-counters">
          <div><small>严格逆序事件</small><b>{{num(d.summary.total_strict_inversion_events as number,0)}}</b></div>
          <div><small>并列变化事件</small><b>{{num(d.summary.tie_change_events as number,0)}}</b></div>
          <div><small>影响最大的剔除</small><b>{{d.summary.most_influential_excluded_code}}</b></div>
        </div>
        <button v-if="d.status==='completed'" class="text-link" @click="emit('open',d.id)">查看范数、理想点变化与反例 ↗</button>
        <p v-else-if="['queued','running'].includes(d.status)" class="working">每个候选一个检查点，中断后自动续跑，页面每2.5秒刷新进度。</p>
      </article>
    </div>

    <template v-if="completedDetail">
      <div class="loo-detail" :key="completedDetail.result.diagnosis_fingerprint">
        <div class="result-byline">
          <b>诊断 #{{(current as DiagnosisSummary).id}} · 沿用 {{completedDetail.result.scenarios_reused}} 个权重情景</b>
          <span>剔除 {{completedDetail.result.excluded_candidates}} 份 · 每次剩余 {{completedDetail.result.remaining_per_diagnosis}} 份</span>
        </div>
        <p v-if="!completedDetail.result.baseline_recheck.stored_scenarios_match_recompute" class="error-text">基线复算与归档情景得分不一致（最大差 {{num(completedDetail.result.baseline_recheck.max_abs_score_gap,8)}}），请勿采信本诊断。</p>
        <p v-else class="notice">基线复算与原权衡归档的 {{completedDetail.result.scenarios_reused}} 个情景得分逐元素一致（最大差 {{num(completedDetail.result.baseline_recheck.max_abs_score_gap,8)}}），诊断可完整复算。</p>

        <h3 class="ruled-title">跨剔除汇总 <small>两两试样的逆序/并列频率</small></h3>
        <div class="table-scroll">
          <table class="sample-table">
            <thead><tr><th>试样对</th><th>观察到的剔除数</th><th>严格逆序事件</th><th>严格逆序频率</th><th>并列变化事件</th><th>并列变化频率</th></tr></thead>
            <tbody>
              <tr v-for="ps in topPairs" :key="ps.pair.join('|')">
                <td><b>{{ps.pair[0]}} ↔ {{ps.pair[1]}}</b></td>
                <td>{{ps.exclusions_observed}}</td>
                <td>{{num(ps.strict_inversion_events,0)}}</td>
                <td>{{percent(ps.strict_inversion_frequency)}}</td>
                <td>{{num(ps.tie_change_events,0)}}</td>
                <td>{{percent(ps.tie_change_frequency)}}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted">每个试样对在保留它的 {{completedDetail.result.excluded_candidates-2}} 次剔除、每次 {{completedDetail.result.scenarios_reused}} 个情景下比较；两类事件互斥。并列仅指贴近度计算值完全相等（平均并列名次），不以编号打破。</p>

        <h3 class="ruled-title">逐份剔除的依据 <small>范数与理想点变化</small></h3>
        <label class="inline-select">被剔除候选
          <select aria-label="选择被剔除候选" :value="excludedCode" @change="pickBlock(($event.target as HTMLSelectElement).value)">
            <option v-for="b in headers" :key="b.excluded_code" :value="b.excluded_code">{{b.excluded_code}} {{b.excluded_name}}</option>
          </select>
        </label>

        <template v-if="selectedHeader">
          <div class="loo-counters">
            <div><small>本剔除的严格逆序事件</small><b>{{num(selectedHeader.strict_inversion_events,0)}}</b></div>
            <div><small>本剔除的并列变化事件</small><b>{{num(selectedHeader.tie_change_events,0)}}</b></div>
            <div><small>中心权重严格逆序对</small><b>{{num(selectedHeader.center_strict_inversion_pairs,0)}}</b></div>
            <div><small>中心权重并列变化对</small><b>{{num(selectedHeader.center_tie_change_pairs,0)}}</b></div>
            <div><small>发生变化的试样对</small><b>{{num(selectedHeader.changed_pair_count,0)}}</b></div>
          </div>
          <div class="table-scroll">
            <table class="distance-table">
              <thead><tr><th>指标</th><th>范数(剔除前)</th><th>范数(剔除后)</th><th>范数变化</th><th>正理想点变化</th><th>负理想点变化</th></tr></thead>
              <tbody>
                <tr v-for="(c,j) in criteria" :key="c">
                  <td><b>{{c}}</b></td>
                  <td>{{num(selectedHeader.norms_before[j],6)}}</td>
                  <td>{{num(selectedHeader.norms_after[j],6)}}</td>
                  <td :class="{'loo-shift':Math.abs(selectedHeader.norm_delta[j])>0}">{{num(selectedHeader.norm_delta[j],6)}}</td>
                  <td :class="{'loo-shift':Math.abs(selectedHeader.ideal_best_delta[j])>0}">{{num(selectedHeader.ideal_best_delta[j],6)}}</td>
                  <td :class="{'loo-shift':Math.abs(selectedHeader.ideal_worst_delta[j])>0}">{{num(selectedHeader.ideal_worst_delta[j],6)}}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <h4 class="ruled-title" style="font-size:18px">本剔除下发生变化的试样对 <small>严格逆序 / 并列变化分开 · 按需读取</small></h4>
          <p v-if="detailError" class="error-text">{{detailError}}</p>
          <p v-if="detailLoading" class="muted">读取检查点…</p>
          <div v-else-if="changedPairs.length" class="table-scroll">
            <table class="sample-table">
              <thead><tr><th>试样对</th><th>中心权重变化</th><th>中心关系(前→后)</th><th>严格逆序(次/频率)</th><th>并列变化(次/频率)</th></tr></thead>
              <tbody>
                <tr v-for="pr in changedPairs" :key="pr.pair.join('|')">
                  <td><b>{{pr.pair[0]}} ↔ {{pr.pair[1]}}</b></td>
                  <td><span :class="['direction',pr.center_change==='strict_inversion'?'':'cost']">{{relationLabel[pr.center_change]}}</span></td>
                  <td>{{relationLabel[pr.center_relation_before]}} → {{relationLabel[pr.center_relation_after]}}</td>
                  <td>{{num(pr.strict_inversion_count,0)}} / {{percent(pr.strict_inversion_frequency)}}</td>
                  <td>{{num(pr.tie_change_count,0)}} / {{percent(pr.tie_change_frequency)}}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="muted">该剔除下没有发生变化的试样对。</p>
          <p class="muted">完整反例可在页面下方按需展开；全部证据按剔除候选保存在产物目录 <code>/artifacts/diagnosis-{{(current as DiagnosisSummary).id}}/blocks/</code>，清单为 <code>manifest.json</code>。数据库只保存计数与范数/理想点变化，避免大 JSON，便于快速查阅与续跑。</p>

          <div class="loo-evidence">
            <div class="section-line" style="padding-bottom:8px">
              <h4 class="ruled-title" style="border:0;padding:0;margin:0;font-size:18px">可复算反例依据 <small>来自共享卷产物，按需加载</small></h4>
              <button class="small-button" :disabled="evidenceLoading" @click="loadEvidence">{{evidenceLoading?'读取中…':'加载「'+excludedCode+'」反例'}}</button>
            </div>
            <p v-if="evidenceError" class="error-text">{{evidenceError}}</p>
            <p v-if="!evidenceBlock&&!evidenceLoading&&!evidenceError" class="muted">点击按钮读取该剔除下发生变化试样对的情景编号、权重，以及剔除前后的得分与平均名次。</p>
            <template v-if="evidenceBlock">
              <div class="table-scroll">
                <table class="sample-table">
                  <thead><tr><th>试样对</th><th>类型</th><th>情景#</th><th>关系(前→后)</th><th>{{'左试样 得分/名次 (前→后)'}}</th><th>{{'右试样 得分/名次 (前→后)'}}</th><th>情景权重</th></tr></thead>
                  <tbody>
                    <template v-for="pr in evidencePairs" :key="pr.pair.join('|')">
                      <tr v-for="ex in [...pr.examples.strict_inversion,...pr.examples.tie_change]" :key="ex.scenario_index+ex.kind">
                        <td><b>{{pr.pair[0]}} ↔ {{pr.pair[1]}}</b></td>
                        <td><span :class="['direction',ex.kind==='tie_change'?'cost':'']">{{relationLabel[ex.kind]}}</span></td>
                        <td>#{{ex.scenario_repeat}}</td>
                        <td>{{relationLabel[ex.relation_before]}} → {{relationLabel[ex.relation_after]}}</td>
                        <td class="loo-num">{{ex.left.code}}<small>{{num(ex.left.score_before,5)}} / {{num(ex.left.rank_before,1)}} → {{num(ex.left.score_after,5)}} / {{num(ex.left.rank_after,1)}}</small></td>
                        <td class="loo-num">{{ex.right.code}}<small>{{num(ex.right.score_before,5)}} / {{num(ex.right.rank_before,1)}} → {{num(ex.right.score_after,5)}} / {{num(ex.right.rank_after,1)}}</small></td>
                        <td class="loo-num"><small>[{{ex.weights.map(w=>num(w,4)).join(', ')}}]</small></td>
                      </tr>
                    </template>
                  </tbody>
                </table>
              </div>
              <p v-if="!evidencePairs.length" class="muted">该剔除下没有发生变化的试样对。</p>
            </template>
          </div>
        </template>
        <div class="fingerprint">诊断指纹 SHA256 <code>{{completedDetail.result.diagnosis_fingerprint}}</code><span>原权衡指纹 {{completedDetail.result.run_fingerprint}}</span></div>
      </div>
    </template>
  </section>
</template>
