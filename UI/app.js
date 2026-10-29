const VIEWS = {
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

let currentView = "em";
let latestData = {
  status: "",
  running: false,
  running_by_role: {},
  jobs: {
    "Software Engineering Manager": [],
    "Technical Program Manager": [],
  },
};
let lastTableSignature = "";

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
      : `No jobs saved yet. Run ${emptyRun}`;
    root.innerHTML = `<p class="empty">${escapeHtml(empty)}</p>`;
    return;
  }

  const rows = jobs
    .map((job) => {
      let link = "No link";
      if (job.link && job.link !== "Link not found") {
        let label = "Apply on company site";
        try {
          const host = new URL(job.link).hostname;
          if (job.easy_apply) {
            label = "Easy Apply";
          } else if (host.includes("linkedin.com")) {
            label = "LinkedIn listing";
          }
        } catch {
          label = job.easy_apply ? "Easy Apply" : "Apply on company site";
        }
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
  if (payload && payload.jobs) {
    return {
      status: payload.status || "",
      running: Boolean(payload.running),
      running_by_role: payload.running_by_role || {},
      jobs: payload.jobs,
    };
  }
  return {
    status: "",
    running: false,
    running_by_role: {},
    jobs: payload || {},
  };
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

  const emJobs = latestData.jobs["Software Engineering Manager"] || [];
  const tpmJobs = latestData.jobs["Technical Program Manager"] || [];
  document.getElementById("em-count").textContent = `(${emJobs.length})`;
  document.getElementById("tpm-count").textContent = `(${tpmJobs.length})`;

  const statusEl = document.getElementById("live-status");
  if (statusEl) {
    statusEl.textContent = latestData.status || (latestData.running ? "Scraping…" : "Idle");
    statusEl.classList.toggle("done", !latestData.running && !/timed out/i.test(latestData.status || ""));
    statusEl.classList.toggle("error", /timed out/i.test(latestData.status || ""));
  }

  const jobs = latestData.jobs[view.key] || [];
  renderTable(jobs, roleIsRunning(latestData, currentView), view.emptyRun);
}

function setView(viewId, updateHash) {
  if (!VIEWS[viewId] || viewId === currentView) {
    paintView();
    return;
  }
  currentView = viewId;
  lastTableSignature = "";
  if (updateHash) {
    const hash = viewId === "tpm" ? "#/tpm" : "#/em";
    if (window.location.hash !== hash) {
      history.replaceState(null, "", hash);
    }
  }
  paintView();
}

function viewFromHash() {
  const hash = (window.location.hash || "").toLowerCase();
  return hash.includes("tpm") ? "tpm" : "em";
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
      status: "Run an agent to get live updates: python BackEnd\\em_agent.py",
      running: false,
      jobs: window.JOB_DATA || {},
    });
  }
}

document.querySelector(".nav-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view]");
  if (!button) {
    return;
  }
  event.preventDefault();
  setView(button.dataset.view, true);
});

window.addEventListener("hashchange", () => {
  setView(viewFromHash(), false);
});

currentView = viewFromHash();
pullLiveJobs();
setInterval(pullLiveJobs, 1500);
