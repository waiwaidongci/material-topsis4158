import {request,post} from './client';import type {Diagnosis,DiagnosisSummary,LooEvidence,LooBlock} from '../types/domain';
export const list=(runId?:number)=>request<DiagnosisSummary[]>('/diagnoses'+(runId?`?run_id=${runId}`:''));
export const create=(data:{name:string;run_id:number})=>post<Diagnosis>('/diagnoses',data);
export const detail=(id:number)=>request<Diagnosis>('/diagnoses/'+id);
export const block=(id:number,excludedCode:string)=>request<LooBlock>('/diagnoses/'+id+'/blocks/'+encodeURIComponent(excludedCode));
export const evidence=(id:number,excludedCode?:string)=>request<LooEvidence>('/diagnoses/'+id+'/evidence'+(excludedCode?`?excluded_code=${encodeURIComponent(excludedCode)}`:''));
