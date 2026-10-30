const VIEWS = {
  resumes: {
    title: "Resumes",
  },
  em: {
    key: "Software Engineering Manager",
    title: "Engineering Manager",
    emptyRun: "python BackEnd\\em_agent.py",
  },
  tpm: {
    key: "Technical Program Manager",
    title: "Technical Program Manager",
    emptyRun: "python BackEnd\\tpm_agent.py",
  },
};

let currentView = "resumes";
let latestData = {
  status: "",
  running: false,
  running_by_role: {},
  agent_procs: { em: false, tpm: false },
  jobs: {
    "Software Engineering Manager": [],
    "Technical Program Manager": [],
  },
};
let lastTableSignature = "";
let lastChatSignature = "";
let chatOpen = false;
let currentTimeRange = "week";
let currentMaxPulls = 50;

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function ratingClass(score) {
  if (score == null || score === "") return "low";
  if (score >= 70) return "good";
  if (score >= 45) return "mid";
  return "low";
}

function ratingLabel(score) {
  if (score == null || score === "") return "n/a";
  return `${score}/100`;
}

function dash(value) {
  const text = String(value ?? "").trim();
  return text ? escapeHtml(text) : "—";
}

const MIN_RATING = 50;

function jobsAtOrAboveMin(jobs) {
  return (jobs || []).filter(
    (job) =>
      job.reassessed || job.rating == null || job.rating === "" || job.rating >= MIN_RATING
  );
}

function roleIsRunning(data, viewId) {
  const role = VIEWS[viewId].key;
  if (data.running_by_role && role in data.running_by_role) {
    return Boolean(data.running_by_role[role]);
  }
  return Boolean(data.running);
}

function renderTable(jobs, running, emptyRun) {
  const root = document.getElementById("jobs-table");
  const signature = JSON.stringify({ jobs, running, view: currentView });
  if (signature === lastTableSignature) {
    return;
  }
  lastTableSignature = signature;

  if (!jobs.length) {
    const empty = running
      ? "Waiting for the scraper to find jobs…"
      : `No jobs saved yet. Upload a resume, then run ${emptyRun}`;
    root.innerHTML = `<p class="empty">${escapeHtml(empty)}</p>`;
    return;
  }

  const rows = jobs
    .map((job) => {
      let link = "No link";
      if (job.link && job.link !== "Link not found") {
        let label = "Open listing";
        try {
          const host = new URL(job.link).hostname;
          if (host.includes("instahyre.com")) {
            label = "Open on Instahyre";
          } else if (host.includes("linkedin.com")) {
            label = job.easy_apply ? "Easy Apply" : "Open on LinkedIn";
          } else if (job.easy_apply) {
            label = "Easy Apply";
          }
        } catch {
          label = job.easy_apply ? "Easy Apply" : "Open listing";
        }
        link = `<a href="${escapeHtml(job.link)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      }
      return `
        <tr>
          <td><span class="job-title">${escapeHtml(job.title || "Untitled")}</span></td>
          <td>${dash(job.company)}</td>
          <td>${link}</td>
          <td class="rating ${ratingClass(job.rating)}">${ratingLabel(job.rating)}</td>
          <td class="col-tailor">
            <button
              type="button"
              class="tailor-btn"
              data-title="${escapeHtml(job.title || "")}"
              data-company="${escapeHtml(job.company || "")}"
              data-link="${escapeHtml(job.link || "")}"
            >Tailor resume</button>
          </td>
          <td class="col-reassess">
            <button
              type="button"
              class="tailor-btn reassess-btn"
              data-title="${escapeHtml(job.title || "")}"
              data-company="${escapeHtml(job.company || "")}"
              data-link="${escapeHtml(job.link || "")}"
            >Re-assess</button>
            ${job.reassessed ? '<span class="reassessed-tag">Re-analyzed</span>' : ""}
          </td>
          <td class="col-why">${dash(job.why)}</td>
          <td class="col-weak">${dash(job.weak_vs_jd)}</td>
          <td class="col-note">${dash(job.note)}</td>
        </tr>
      `;
    })
    .join("");

  root.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="col-job">Job</th>
            <th class="col-company">Company</th>
            <th class="col-link">Link</th>
            <th class="col-rating">Rating</th>
            <th class="col-tailor">Resume</th>
            <th class="col-reassess">Re-assess</th>
            <th class="col-why">Why</th>
            <th class="col-weak">Weak vs JD</th>
            <th class="col-note">Note</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function normalizePayload(payload) {
  const settings = (payload && payload.settings) || {};
  const timeRange = settings.time_range === "24h" ? "24h" : "week";
  const maxPulls = Number(settings.max_pulls);
  const pulls = Number.isFinite(maxPulls) ? Math.max(5, Math.min(200, Math.round(maxPulls))) : 50;
  if (payload && payload.jobs) {
    return {
      status: payload.status || "",
      running: Boolean(payload.running),
      running_by_role: payload.running_by_role || {},
      agent_procs: payload.agent_procs || { em: false, tpm: false },
      settings: { time_range: timeRange, max_pulls: pulls },
      jobs: payload.jobs,
      chat: payload.chat || { running: false, messages: [], focus_role: null },
    };
  }
  return {
    status: "",
    running: false,
    running_by_role: {},
    agent_procs: { em: false, tpm: false },
    settings: { time_range: timeRange, max_pulls: pulls },
    jobs: payload || {},
    chat: { running: false, messages: [], focus_role: null },
  };
}

function paintTimeRange(timeRange) {
  currentTimeRange = timeRange === "24h" ? "24h" : "week";
  document.querySelectorAll("[data-time-range]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.timeRange === currentTimeRange);
  });
}

function paintMaxPulls(value) {
  currentMaxPulls = Number.isFinite(Number(value)) ? Math.max(5, Math.min(200, Math.round(Number(value)))) : 50;
  const input = document.getElementById("max-pulls");
  if (input && document.activeElement !== input) {
    input.value = String(currentMaxPulls);
  }
}

function paintResumeCard(role, summary) {
  const meta = document.getElementById(`${role}-resume-meta`);
  const list = document.getElementById(`${role}-skill-list`);
  if (!summary || !summary.uploaded) {
    meta.textContent = "No resume uploaded.";
    list.innerHTML = "";
    return;
  }
  const mode = summary.llm ? "LLM context saved in Assets" : "keyword extract only";
  meta.textContent = `${summary.filename} · ${mode}`;
  const summaryHtml = summary.summary
    ? `<li class="resume-summary">${escapeHtml(summary.summary)}</li>`
    : "";
  const skillsHtml = (summary.skills || [])
    .map((skill) => `<li>${escapeHtml(skill)}</li>`)
    .join("");
  list.innerHTML = summaryHtml + skillsHtml;
  if (summary.llm_error && !summary.llm) {
    document.getElementById(`${role}-resume-hint`).textContent = summary.llm_error;
  }
}

async function refreshResumes() {
  try {
    const response = await fetch(`/api/resumes?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    paintResumeCard("em", data.em);
    paintResumeCard("tpm", data.tpm);
  } catch {
    document.getElementById("em-resume-meta").textContent =
      "Start the UI server to upload: python BackEnd\\ui_server.py";
  }
}

async function uploadResume(role, file) {
  const hint = document.getElementById(`${role}-resume-hint`);
  hint.textContent = "Reading resume and building LLM context…";
  try {
    const response = await fetch(`/api/resume?role=${role}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/pdf",
        "X-Filename": file.name,
      },
      body: file,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    hint.textContent = "Saved to Assets. Agents will use this profile.";
    await refreshResumes();
  } catch (error) {
    hint.textContent = error.message || "Upload failed.";
  }
}

function paintView() {
  const view = VIEWS[currentView];
  document.getElementById("view-title").textContent = view.title;
  document.querySelectorAll(".nav-tab").forEach((button) => {
    const active = button.dataset.view === currentView;
    button.classList.toggle("is-active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });

  paintTimeRange((latestData.settings && latestData.settings.time_range) || currentTimeRange);
  paintMaxPulls((latestData.settings && latestData.settings.max_pulls) || currentMaxPulls);

  const emJobs = jobsAtOrAboveMin(latestData.jobs["Software Engineering Manager"]);
  const tpmJobs = jobsAtOrAboveMin(latestData.jobs["Technical Program Manager"]);
  document.getElementById("em-count").textContent = `(${emJobs.length})`;
  document.getElementById("tpm-count").textContent = `(${tpmJobs.length})`;

  const statusEl = document.getElementById("live-status");
  const jobsPanel = document.getElementById("jobs-panel");
  const resumesPanel = document.getElementById("resumes-panel");
  const isResumes = currentView === "resumes";
  jobsPanel.classList.toggle("is-hidden", isResumes);
  resumesPanel.classList.toggle("is-hidden", !isResumes);

  const headerBtn = document.getElementById("fresh-run-btn");
  headerBtn.classList.toggle("is-hidden", isResumes);
  const procs = latestData.agent_procs || {};
  if (!isResumes) {
    const busy = Boolean(procs[currentView]);
    headerBtn.disabled = busy;
    headerBtn.textContent = busy ? "Running…" : "Fresh run";
  }
  document.querySelectorAll("[data-run-role]").forEach((button) => {
    const busy = Boolean(procs[button.dataset.runRole]);
    button.disabled = busy;
    const label = button.dataset.runRole === "tpm" ? "TPM" : "EM";
    button.textContent = busy ? `Running ${label}…` : `Fresh ${label} run`;
  });

  if (statusEl) {
    if (isResumes) {
      statusEl.textContent = "Upload a PDF for each role. Extracted skills are stored in Assets/.";
      statusEl.classList.remove("done", "error");
    } else {
      statusEl.textContent = latestData.status || (latestData.running ? "Scraping…" : "Idle");
      statusEl.classList.toggle("done", !latestData.running && !/timed out/i.test(latestData.status || ""));
      statusEl.classList.toggle("error", /timed out/i.test(latestData.status || ""));
      const jobs = jobsAtOrAboveMin(latestData.jobs[view.key]);
      renderTable(jobs, roleIsRunning(latestData, currentView) || Boolean(procs[currentView]), view.emptyRun);
    }
  }
  paintChat();
}

function setChatOpen(open) {
  chatOpen = Boolean(open);
  const panel = document.getElementById("chat-panel");
  const toggle = document.getElementById("chat-toggle");
  panel.hidden = !chatOpen;
  toggle.classList.toggle("is-open", chatOpen);
  toggle.setAttribute("aria-expanded", chatOpen ? "true" : "false");
  if (chatOpen) {
    lastChatSignature = "";
    paintChat();
    document.getElementById("chat-input").focus();
  }
}

function paintChat() {
  const chat = latestData.chat || { running: false, messages: [] };
  const send = document.getElementById("chat-send");
  send.disabled = Boolean(chat.running);
  send.textContent = chat.running ? "Working…" : "Send";
  const messages = chat.messages || [];
  const signature = JSON.stringify({ open: chatOpen, running: chat.running, messages });
  if (signature === lastChatSignature) {
    return;
  }
  lastChatSignature = signature;
  const log = document.getElementById("chat-log");
  if (!messages.length) {
    log.innerHTML = `<p class="chat-empty">Ask in plain English. Example: get 10 strong EM fits this week, tailor the top ones, re-score them, and tell me which to apply to. TPM requests go to the TPM agent.</p>`;
    return;
  }
  log.innerHTML = messages
    .map(
      (item) =>
        `<div class="chat-bubble ${item.role === "user" ? "user" : "assistant"}">${escapeHtml(item.text || "")}</div>`
    )
    .join("");
  log.scrollTop = log.scrollHeight;
}

function maybeFocusChatRole() {
  const chat = latestData.chat || {};
  const role = chat.focus_role;
  if (!chat.running || (role !== "em" && role !== "tpm")) {
    return;
  }
  if (currentView === "resumes") {
    setView(role, true);
  }
}

async function sendChatMessage(text) {
  const statusEl = document.getElementById("live-status");
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: text }),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  if (payload.chat) {
    latestData.chat = payload.chat;
    lastChatSignature = "";
    paintChat();
  }
  statusEl.classList.remove("error");
  pullLiveJobs();
}

async function startFreshRun(role) {
  const response = await fetch(`/api/run?role=${role}`, { method: "POST" });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  latestData.agent_procs = latestData.agent_procs || {};
  latestData.agent_procs[role] = true;
  setView(role, true);
  pullLiveJobs();
}

function hashForView(viewId) {
  if (viewId === "tpm") return "#/tpm";
  if (viewId === "em") return "#/em";
  return "#/resumes";
}

function setView(viewId, updateHash) {
  if (!VIEWS[viewId]) {
    return;
  }
  if (viewId !== currentView) {
    lastTableSignature = "";
  }
  currentView = viewId;
  if (updateHash) {
    const hash = hashForView(viewId);
    if (window.location.hash !== hash) {
      history.replaceState(null, "", hash);
    }
  }
  paintView();
  if (viewId === "resumes") {
    refreshResumes();
  }
}

function viewFromHash() {
  const hash = (window.location.hash || "").toLowerCase();
  if (hash.includes("tpm")) return "tpm";
  if (hash.includes("em")) return "em";
  return "resumes";
}

function renderAll(payload) {
  latestData = normalizePayload(payload);
  paintView();
  maybeFocusChatRole();
}

async function pullLiveJobs() {
  try {
    const response = await fetch(`/api/jobs?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    renderAll(await response.json());
  } catch {
    renderAll({
      status: "Run python BackEnd\\ui_server.py or an agent for live updates.",
      running: false,
      jobs: window.JOB_DATA || {},
    });
  }
}

async function setTimeRange(timeRange) {
  paintTimeRange(timeRange);
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ time_range: currentTimeRange, max_pulls: currentMaxPulls }),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  paintTimeRange(payload.time_range || currentTimeRange);
  paintMaxPulls(payload.max_pulls ?? currentMaxPulls);
}

async function setMaxPulls(value) {
  paintMaxPulls(value);
  const response = await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_pulls: currentMaxPulls }),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  paintMaxPulls(payload.max_pulls ?? currentMaxPulls);
}

document.getElementById("max-pulls").addEventListener("change", async (event) => {
  try {
    await setMaxPulls(event.target.value);
  } catch (error) {
    const statusEl = document.getElementById("live-status");
    statusEl.textContent = error.message || "Could not save max pulls.";
    statusEl.classList.add("error");
  }
});

document.querySelector(".nav-range").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-time-range]");
  if (!button) {
    return;
  }
  try {
    await setTimeRange(button.dataset.timeRange);
  } catch (error) {
    const statusEl = document.getElementById("live-status");
    statusEl.textContent = error.message || "Could not save time range.";
    statusEl.classList.add("error");
  }
});

document.querySelector(".nav-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) {
    return;
  }
  event.preventDefault();
  setView(button.dataset.view, true);
});

async function postJobAction(path, button) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      role: currentView,
      title: button.dataset.title || "",
      company: button.dataset.company || "",
      link: button.dataset.link || "",
    }),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

document.getElementById("jobs-table").addEventListener("click", async (event) => {
  if (currentView !== "em" && currentView !== "tpm") {
    return;
  }
  const reassessBtn = event.target.closest(".reassess-btn");
  const tailorBtn = event.target.closest(".tailor-btn");
  const button = reassessBtn || (tailorBtn && !tailorBtn.classList.contains("reassess-btn") ? tailorBtn : null);
  if (!button) {
    return;
  }
  const statusEl = document.getElementById("live-status");
  const isReassess = Boolean(reassessBtn);
  button.disabled = true;
  const original = button.textContent;
  button.textContent = isReassess ? "Re-analyzing…" : "Tailoring…";
  try {
    const payload = await postJobAction(isReassess ? "/api/reassess" : "/api/tailor", button);
    if (isReassess && payload.row) {
      const key = VIEWS[currentView].key;
      const rows = latestData.jobs[key] || [];
      const match = rows.find(
        (job) =>
          job.title === payload.row.title &&
          job.company === payload.row.company &&
          (!payload.row.link || !job.link || job.link === payload.row.link)
      );
      if (match) {
        Object.assign(match, payload.row);
        lastTableSignature = "";
        paintView();
      } else {
        pullLiveJobs();
      }
    }
    statusEl.textContent = isReassess
      ? `Re-analyzed ${button.dataset.title} with tailored resume`
      : `Saved tailored resume: ${payload.filename}`;
    statusEl.classList.remove("error");
    statusEl.classList.add("done");
  } catch (error) {
    statusEl.textContent = error.message || (isReassess ? "Could not re-assess job." : "Could not tailor resume.");
    statusEl.classList.add("error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
});

document.getElementById("fresh-run-btn").addEventListener("click", async () => {
  if (currentView !== "em" && currentView !== "tpm") {
    return;
  }
  const button = document.getElementById("fresh-run-btn");
  button.disabled = true;
  try {
    await startFreshRun(currentView);
  } catch (error) {
    button.disabled = false;
    const statusEl = document.getElementById("live-status");
    statusEl.textContent = error.message || "Could not start a fresh run.";
    statusEl.classList.add("error");
  }
});

document.querySelectorAll("[data-run-role]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await startFreshRun(button.dataset.runRole);
    } catch (error) {
      const hint = document.getElementById(`${button.dataset.runRole}-resume-hint`);
      if (hint) {
        hint.textContent = error.message || "Could not start a fresh run.";
      }
    }
  });
});

document.getElementById("em-resume-input").addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  if (file) {
    uploadResume("em", file);
  }
  event.target.value = "";
});

document.getElementById("tpm-resume-input").addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  if (file) {
    uploadResume("tpm", file);
  }
  event.target.value = "";
});

document.getElementById("chat-toggle").addEventListener("click", () => {
  setChatOpen(!chatOpen);
});

document.getElementById("chat-close").addEventListener("click", () => {
  setChatOpen(false);
});

document.getElementById("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text || (latestData.chat && latestData.chat.running)) {
    return;
  }
  setChatOpen(true);
  try {
    await sendChatMessage(text);
    input.value = "";
  } catch (error) {
    latestData.chat = latestData.chat || { messages: [] };
    latestData.chat.messages = (latestData.chat.messages || []).concat({
      role: "assistant",
      text: error.message || "Could not start that instruction.",
    });
    lastChatSignature = "";
    paintChat();
  }
});

document.getElementById("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.getElementById("chat-form").requestSubmit();
  }
});

window.addEventListener("hashchange", () => {
  setView(viewFromHash(), false);
});

currentView = viewFromHash();
pullLiveJobs();
refreshResumes();
setInterval(pullLiveJobs, 1500);
