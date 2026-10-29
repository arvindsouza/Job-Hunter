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

function renderTable(containerId, jobs, running) {
  const root = document.getElementById(containerId);
  const count = document.getElementById(`${containerId}-count`);
  if (count) {
    count.textContent = `(${jobs.length})`;
  }

  if (!jobs.length) {
    const empty = running
      ? "Waiting for the scraper to find jobs…"
      : "No jobs saved yet. Run linkedin_agent.py to start the live UI.";
    root.innerHTML = `<p class="empty">${empty}</p>`;
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
      jobs: payload.jobs,
    };
  }
  return {
    status: "",
    running: false,
    jobs: payload || {},
  };
}

function renderAll(payload) {
  const data = normalizePayload(payload);
  const statusEl = document.getElementById("live-status");
  if (statusEl) {
    statusEl.textContent = data.status || (data.running ? "Scraping…" : "Idle");
    statusEl.classList.toggle("done", !data.running && !/timed out/i.test(data.status || ""));
    statusEl.classList.toggle("error", /timed out/i.test(data.status || ""));
  }
  renderTable("sem-table", data.jobs["Software Engineering Manager"] || [], data.running);
  renderTable("tpm-table", data.jobs["Technical Program Manager"] || [], data.running);
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
      status: "Open this page from the agent (python linkedin_agent.py) to get live updates.",
      running: false,
      jobs: window.JOB_DATA || {},
    });
  }
}

pullLiveJobs();
setInterval(pullLiveJobs, 1500);
