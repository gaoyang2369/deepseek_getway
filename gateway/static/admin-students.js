'use strict';
async function studentAdminView(){
  const accounts=await api('/students/accounts');
  const people=await api('/students');
  const accountFor=id=>accounts.find(a=>a.student_id===id);
  function showTemporary(title,result){
    $('#modal-title').textContent=title;
    $('#modal-fields').replaceChildren(node('p','临时密码仅在此次响应显示。请核实身份后通过可靠渠道单独交给对应同学。','key-warning'),node('pre',result),node('p','学生入口：'+location.origin+'/student'));
    $('#modal-submit').hidden=true;submitAction=null;
    $('#modal-error').hidden=true;if(!$('#modal').open)$('#modal').showModal();
  }
  const importButton=button('＋ 批量导入学号',()=>{
    if(!cache.teams.length)throw new Error('请先创建项目组');
    openForm('导入学生账号',[node('p','每行填写“学号”或“学号,姓名”，保留前导零，每批最多 100 人。重复导入只补齐账户和成员关系，不重置现有密码、预算或 Key。','section-note'),field('项目组','team_id','text',cache.teams[0].id,cache.teams.map(t=>[t.id,t.name])),field('学号名单','roster','textarea'),field('初始密码方式','initial_password_mode','text',cache.config.initial_password_mode,cache.config.demo?[['student_number','学号（当前模拟环境）'],['random','随机临时密码']]:[['random','随机临时密码']]),field('个人预算 / 元','budget','number','10'),field('周期','period','text','month',[['month','自然月'],['day','自然日']]),field('允许模型（英文逗号分隔）','models','text',cache.teams[0].models.join(',')),field('接入到期时间（可留空）','expires_at','datetime-local')],async d=>{
      const students=d.roster.split(/\r?\n/).map(s=>s.trim()).filter(Boolean).map(line=>{const parts=line.split(/[,，\t]/).map(s=>s.trim());if(parts.length>2||!parts[0])throw new Error('名单每行只接受学号，或学号和姓名两列');return {student_number:parts[0],...(parts[1]?{name:parts[1]}:{})};});
      const result=await api('/students/import','POST',{operation_id:$('#modal-form').dataset.operation,team_id:d.team_id,students,initial_password_mode:d.initial_password_mode,budget:yuan(d.budget),period:d.period,models:models(d.models),expires_at:d.expires_at?new Date(d.expires_at).toISOString():null});
      if(result.initial_passwords.length)showTemporary('初始密码（仅显示一次）',result.initial_passwords.map(s=>s.student_number+'\t'+s.password).join('\n'));
      else closeModal();
      await load();notice(result.replayed?'此次导入已处理，原密码和预算保留。':`已导入 ${result.students.length} 人，网页登录已自动开通，由学生自行登录领取 Key。`,'success');
    });
    $('#modal-form').dataset.operation=operationId();
    $('#modal-fields [name=expires_at]').required=false;
  });
  return [toolbar('学生自助入口',importButton),node('p','学生访问 '+location.origin+'/student。首次登录必须改密，之后自行领取个人 Key。忘记密码时，通过邮箱联系管理员，管理员核实身份后重置。重置会撤销该学生的全部旧会话与 API Key，保留已用预算。','section-note'),table(['姓名','学号','账号状态','操作'],people.map(p=>{const a=accountFor(p.student_id);return [p.name,p.student_number,a?(a.must_change_password?'待首次改密':'已设置密码'):'待设置初始密码',actions(button('重置密码',()=>openForm('核实身份后生成临时密码',[node('p','旧会话和该学生全部旧 API Key 将失效。已发送请求仍可能执行，预算不会清零。','key-warning'),field('身份核实记录与重置原因','reason','textarea')],async d=>{const result=await api('/students/'+p.student_id+'/reset-password','POST',{reason:d.reason});showTemporary('临时密码（仅显示一次）',p.student_number+'\t'+result.temporary_password);await load();})),button('删除学生',()=>deleteEntry('/students/'+p.student_id,'学生“'+p.name+' / '+p.student_number+'”','该学生在所有项目组的成员关系会被移除，全部 API Key 和登录会话将失效。项目组及其他学生保留。'),'danger'))];}))];
}
