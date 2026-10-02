"use strict";

const $ = (id) => document.getElementById(id);

const EXAMPLE = {
  sender: [
    { name: "Cmd", type: { kind: "record", fields: [
      { name: "id", type: { kind: "int" }, required: true },
      { name: "payload", type: { kind: "ref", name: "Payload" }, required: true },
      { name: "note", type: { kind: "text" }, required: false }
    ]}},
    { name: "Payload", type: { kind: "variant", tags: [
      { label: "Data", type: { kind: "record", fields: [
        { name: "value", type: { kind: "int" }, required: true },
        { name: "next", type: { kind: "ref", name: "Payload" }, required: false }
      ]}},
      { label: "Ack", type: { kind: "record", fields: [
        { name: "ok", type: { kind: "bool" }, required: true }
      ]}}
    ]}}
  ],
  receiver: [
    { name: "Cmd", type: { kind: "record", fields: [
      { name: "id", type: { kind: "int" }, required: true },
      { name: "payload", type: { kind: "ref", name: "Payload" }, required: true }
    ]}},
    { name: "Payload", type: { kind: "variant", tags: [
      { label: "Data", type: { kind: "record", fields: [
        { name: "value", type: { kind: "int" }, required: true },
        { name: "next", type: { kind: "ref", name: "Payload" }, required: false }
      ]}},
      { label: "Ack", type: { kind: "record", fields: [
        { name: "ok", type: { kind: "bool" }, required: true }
      ]}},
      { label: "Reset", type: { kind: "record", fields: [
        { name: "at", type: { kind: "int" }, required: true }
      ]}}
    ]}}
  ]
};

const MIGRATION_EXAMPLE = {
  // 来源审计即上方示例 CMD-AUDIT-2026-001；新版仅改名并重排声明/字段/标签顺序。
  new_sender: [
    { name: "Pl", type: { kind: "variant", tags: [
      { label: "Ack", type: { kind: "record", fields: [
        { name: "ok", type: { kind: "bool" }, required: true }
      ]}},
      { label: "Data", type: { kind: "record", fields: [
        { name: "next", type: { kind: "ref", name: "Pl" }, required: false },
        { name: "value", type: { kind: "int" }, required: true }
      ]}}
    ]}},
    { name: "Command", type: { kind: "record", fields: [
      { name: "note", type: { kind: "text" }, required: false },
      { name: "payload", type: { kind: "ref", name: "Pl" }, required: true },
      { name: "id", type: { kind: "int" }, required: true }
    ]}}
  ],
  new_receiver: [
    { name: "Command", type: { kind: "record", fields: [
      { name: "payload", type: { kind: "ref", name: "Pl" }, required: true },
      { name: "id", type: { kind: "int" }, required: true }
    ]}},
    { name: "Pl", type: { kind: "variant", tags: [
      { label: "Reset", type: { kind: "record", fields: [
        { name: "at", type: { kind: "int" }, required: true }
      ]}},
      { label: "Data", type: { kind: "record", fields: [
        { name: "value", type: { kind: "int" }, required: true },
        { name: "next", type: { kind: "ref", name: "Pl" }, required: false }
      ]}},
      { label: "Ack", type: { kind: "record", fields: [
        { name: "ok", type: { kind: "bool" }, required: true }
      ]}}
    ]}}
  ]
};

function hide(id) { $(id).classList.add("hidden"); }
function show(id) { $(id).classList.remove("hidden"); }

function resetPanels() {
  ["issuesCard", "resultCard", "conflictCard"].forEach(hide);
  $("editorError").textContent = "";
}

function loadExample() {
  $("auditId").value = "CMD-AUDIT-2026-001";
  $("rootName").value = "Cmd";
  $("senderTypes").value = JSON.stringify(EXAMPLE.sender, null, 2);
  $("receiverTypes").value = JSON.stringify(EXAMPLE.receiver, null, 2);
}

function readEditor() {
  let sender, receiver;
  try {
    sender = JSON.parse($("senderTypes").value);
  } catch (e) {
    throw new Error(`发送端声明不是合法 JSON：${e.message}`);
  }
  try {
    receiver = JSON.parse($("receiverTypes").value);
  } catch (e) {
    throw new Error(`接收端声明不是合法 JSON：${e.message}`);
  }
  const body = {
    audit_id: $("auditId").value.trim(),
    sender_types: sender,
    receiver_types: receiver
  };
  const root = $("rootName").value.trim();
  if (root) body.root_name = root;
  return body;
}

function renderIssues(issues) {
  const ul = $("issuesList");
  ul.innerHTML = "";
  for (const it of issues) {
    const li = document.createElement("li");
    li.textContent = it;
    ul.appendChild(li);
  }
  show("issuesCard");
}

function renderConclusion(c, extra) {
  const title = $("resultTitle");
  if (c.compatible) {
    title.textContent = "✔ 兼容：发送端全部可能产出的递归载荷均被接收端接受";
    title.className = "ok";
  } else {
    title.textContent = "✘ 不兼容：存在被接收端拒收的载荷形态";
    title.className = "bad";
  }
  const meta = [
    `审计标识：${c.audit_id}`,
    `根类型：${c.root}`,
    `冻结时间(UTC)：${c.frozen_at}`,
    `契约指纹：${c.contract_fingerprint.slice(0, 16)}…`
  ];
  if (extra && extra.resubmitted_same_contract) meta.push("本次为相同契约重传，读取原冻结结论");
  $("resultMeta").textContent = meta.join("　｜　");

  if (c.mismatch) {
    $("mmPath").textContent = c.mismatch.path;
    $("mmCode").textContent = c.mismatch.code;
    $("mmMsg").textContent = c.mismatch.message;
    $("mmDetail").textContent = JSON.stringify(c.mismatch.detail || {}, null, 2);
    show("mismatchBox");
  } else {
    hide("mismatchBox");
  }

  const body = $("recycleBody");
  body.innerHTML = "";
  if (c.recycled && c.recycled.length) {
    for (const r of c.recycled) {
      const tr = document.createElement("tr");
      [r.path, r.pair, r.first_seen_at].forEach((v) => {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    }
    show("recycleBox");
  } else {
    hide("recycleBox");
  }
  $("rawConclusion").textContent = JSON.stringify(c, null, 2);
  show("resultCard");
}

async function submitAudit() {
  resetPanels();
  let body;
  try {
    body = readEditor();
  } catch (e) {
    $("editorError").textContent = e.message;
    return;
  }
  const resp = await fetch("/api/audits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (resp.status === 400 && data.issues) {
    renderIssues(data.issues);
    return;
  }
  if (resp.status === 409) {
    $("conflictMsg").textContent = data.message || "契约指纹不一致，提交被拒绝。";
    show("conflictCard");
    return;
  }
  if (!resp.ok || !data.conclusion) {
    $("editorError").textContent = `请求失败：${resp.status} ${JSON.stringify(data)}`;
    return;
  }
  renderConclusion(data.conclusion, data);
}

async function reopen() {
  resetPanels();
  const id = $("auditId").value.trim();
  if (!id) {
    $("editorError").textContent = "请先填写稳定审计标识";
    return;
  }
  const resp = await fetch(`/api/audits/${encodeURIComponent(id)}`);
  const data = await resp.json();
  if (resp.status === 404) {
    $("editorError").textContent = "该审计标识尚无冻结结论";
    return;
  }
  if (!resp.ok) {
    $("editorError").textContent = `请求失败：${resp.status}`;
    return;
  }
  renderConclusion(data.conclusion, null);
}

function resetMigrationPanels() {
  ["migrationIssuesCard", "migrationRejectCard", "migrationConflictCard",
   "migrationResultCard"].forEach(hide);
  $("migrationError").textContent = "";
}

function loadMigrationExample() {
  $("migrationId").value = "CMD-MIGRATION-2026-001";
  $("sourceAuditId").value = "CMD-AUDIT-2026-001";
  $("newRootName").value = "Command";
  $("newSenderTypes").value = JSON.stringify(MIGRATION_EXAMPLE.new_sender, null, 2);
  $("newReceiverTypes").value = JSON.stringify(MIGRATION_EXAMPLE.new_receiver, null, 2);
}

function readMigrationEditor() {
  let newSender, newReceiver;
  try {
    newSender = JSON.parse($("newSenderTypes").value);
  } catch (e) {
    throw new Error(`新版发送端声明不是合法 JSON：${e.message}`);
  }
  try {
    newReceiver = JSON.parse($("newReceiverTypes").value);
  } catch (e) {
    throw new Error(`新版接收端声明不是合法 JSON：${e.message}`);
  }
  const body = {
    migration_id: $("migrationId").value.trim(),
    source_audit_id: $("sourceAuditId").value.trim(),
    new_root_name: $("newRootName").value.trim(),
    new_sender_types: newSender,
    new_receiver_types: newReceiver
  };
  return body;
}

function renderStringList(listId, items) {
  const ul = $(listId);
  ul.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.textContent = it;
    ul.appendChild(li);
  }
}

function renderBijection(tbodyId, pairs) {
  const body = $(tbodyId);
  body.innerHTML = "";
  for (const p of pairs) {
    const tr = document.createElement("tr");
    [p.source, p.target].forEach((v) => {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
}

function renderMigration(m, extra) {
  $("migrationResultTitle").textContent =
    "✔ 迁移签证已签发：新版两侧契约与来源完全结构等价，名称双射完整且唯一";
  $("migrationResultTitle").className = "ok";
  const meta = [
    `迁移标识：${m.migration_id}`,
    `来源审计：${m.source_audit_id}`,
    `根类型：${m.source_root} → ${m.new_root}`,
    `冻结时间(UTC)：${m.frozen_at}`,
    `签证指纹：${m.migration_fingerprint.slice(0, 16)}…`
  ];
  if (extra && extra.resubmitted_same_request) {
    meta.push("本次为相同请求重传，读取原冻结签证");
  }
  $("migrationMeta").textContent = meta.join("　｜　");
  renderBijection("senderBijectionBody", m.sender_bijection);
  renderBijection("receiverBijectionBody", m.receiver_bijection);
  $("rawMigration").textContent = JSON.stringify(m, null, 2);
  show("migrationResultCard");
}

async function submitMigration() {
  resetMigrationPanels();
  let body;
  try {
    body = readMigrationEditor();
  } catch (e) {
    $("migrationError").textContent = e.message;
    return;
  }
  const resp = await fetch("/api/migrations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const data = await resp.json();
  if (resp.status === 400 && data.issues) {
    renderStringList("migrationIssuesList", data.issues);
    show("migrationIssuesCard");
    return;
  }
  if (resp.status === 422 && data.reasons) {
    renderStringList("migrationRejectList", data.reasons);
    show("migrationRejectCard");
    return;
  }
  if (resp.status === 409) {
    $("migrationConflictMsg").textContent =
      data.message || "来源或新版契约与已冻结签证不一致，请求被拒绝。";
    show("migrationConflictCard");
    return;
  }
  if (!resp.ok || !data.migration) {
    $("migrationError").textContent = `请求失败：${resp.status} ${JSON.stringify(data)}`;
    return;
  }
  renderMigration(data.migration, data);
}

async function reopenMigration() {
  resetMigrationPanels();
  const id = $("migrationId").value.trim();
  if (!id) {
    $("migrationError").textContent = "请先填写迁移标识";
    return;
  }
  const resp = await fetch(`/api/migrations/${encodeURIComponent(id)}`);
  const data = await resp.json();
  if (resp.status === 404) {
    $("migrationError").textContent = "该迁移标识尚无冻结签证";
    return;
  }
  if (!resp.ok) {
    $("migrationError").textContent = `请求失败：${resp.status}`;
    return;
  }
  $("sourceAuditId").value = data.migration.source_audit_id;
  $("newRootName").value = data.migration.new_root;
  renderMigration(data.migration, null);
}

$("loadExample").addEventListener("click", loadExample);
$("submitBtn").addEventListener("click", submitAudit);
$("reopenBtn").addEventListener("click", reopen);
$("loadMigrationExample").addEventListener("click", loadMigrationExample);
$("submitMigrationBtn").addEventListener("click", submitMigration);
$("reopenMigrationBtn").addEventListener("click", reopenMigration);
loadExample();
loadMigrationExample();
