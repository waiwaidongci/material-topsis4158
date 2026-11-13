import {defineStore} from 'pinia';import {ref} from 'vue';import * as diagnosesApi from '../api/diagnoses';import type {Diagnosis,DiagnosisSummary} from '../types/domain';
export const useDiagnosis=defineStore('diagnosis',()=>{
  const diagnoses=ref<DiagnosisSummary[]>([]);
  const current=ref<Diagnosis|null>(null);
  const selected=ref(0);
  async function load(runId?:number){diagnoses.value=await diagnosesApi.list(runId);}
  async function select(id:number){selected.value=id;current.value=null;if(id)current.value=await diagnosesApi.detail(id);}
  async function queue(data:{name:string;run_id:number}){const created=await diagnosesApi.create(data);await load(data.run_id);await select(created.id);return created;}
  return {diagnoses,current,selected,load,select,queue};
});
