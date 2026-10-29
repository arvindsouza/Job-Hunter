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
let currentTimeRange = "week";

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
  return (jobs || []).filter((job) => job.rating == null || job.rating === "" || job.rating >= MIN_RATING);
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
        const label = job.easy_apply ? "Easy Apply" : "Open on LinkedIn";
        link = `<a href="${escapeHtml(job.link)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      }
      return `
        <tr>
          <td><span class="job-title">${escapeHtml(job.title || "Untitled")}</span></td>
          <td>${dash(job.company)}</td>
          <td>${link}</td>
          <td class="rating ${ratingClass(job.rating)}">${ratingLabel(job.rating)}</td>
          <td>${dash(job.why)}</td>
          <td>${dash(job.weak_vs_jd)}</td>
          <td>${dash(job.note)}</td>
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
  if (payload && payload.jobs) {
    return {
      status: payload.status || "",
      running: Boolean(payload.running),
      running_by_role: payload.running_by_role || {},
      agent_procs: payload.agent_procs || { em: false, tpm: false },
      settings: { time_range: timeRange },
      jobs: payload.jobs,
    };
  }
  return {
    status: "",
    running: false,
    running_by_role: {},
    agent_procs: { em: false, tpm: false },
    settings: { time_range: timeRange },
    jobs: payload || {},
  };
}

function paintTimeRange(timeRange) {
  currentTimeRange = timeRange === "24h" ? "24h" : "week";
  document.querySelectorAll("[data-time-range]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.timeRange === currentTimeRange);
  });
}

function paintResumeCard(role, summary) {
  const meta = document.getElementById(`${role}-resume-meta`);
  const list = document.getElementById(`${role}-skill-list`);
  if (!summary || !summary.uploaded) {
    meta.textContent = "No resume uploaded.";
    list.innerHTML = "";
    return;
  }
  meta.textContent = `${summary.filename} · ${summary.skill_count} skill(s) extracted`;
  list.innerHTML = (summary.skills || [])
    .map((skill) => `<li>${escapeHtml(skill)}</li>`)
    .join("");
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
  hint.textContent = "Extracting skills…";
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
    body: JSON.stringify({ time_range: currentTimeRange }),
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
}

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

window.addEventListener("hashchange", () => {
  setView(viewFromHash(), false);
});

currentView = viewFromHash();
pullLiveJobs();
refreshResumes();
setInterval(pullLiveJobs, 1500);
