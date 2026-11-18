#!/usr/bin/env Rscript
# Fit Rasch (1PL) IRT model; optional fixed-item-parameter calibration (FIPC) for anchors.

args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 3) {
  cat("Usage: Rscript run_rasch.R <responses.csv> <item_params.csv> <fit_summary.json> [anchor_params.csv] [new_item_ids.txt]\n")
  quit(status = 1)
}

responses_path <- args[1]
item_params_path <- args[2]
fit_summary_path <- args[3]
anchor_params_path <- if (length(args) >= 4 && nzchar(args[4])) args[4] else NA
new_item_ids_path <- if (length(args) >= 5 && nzchar(args[5])) args[5] else NA

if (!require(mirt, quietly = TRUE)) {
  cat("Installing mirt package...\n")
  install.packages('mirt', repos = 'https://cloud.r-project.org', quiet = TRUE)
  library(mirt)
}

library(jsonlite)

cat("Loading responses from:", responses_path, "\n")

data <- read.csv(responses_path, header = TRUE, check.names = FALSE)
if ("respondent_id" %in% colnames(data)) {
  rownames(data) <- data$respondent_id
  data <- data[, setdiff(colnames(data), "respondent_id"), drop = FALSE]
}

original_items <- colnames(data)

if (!all(unlist(data) %in% c(0, 1, NA))) {
  stop("Data must contain only 0, 1, or NA")
}

cat("Data shape:", nrow(data), "respondents ×", ncol(data), "items\n")

item_variances <- apply(data, 2, function(x) length(unique(na.omit(x))))
invariant_items <- names(item_variances[item_variances < 2])

if (length(invariant_items) > 0) {
  cat("Warning: Removing", length(invariant_items), "items with no variance:", paste(invariant_items, collapse = ", "), "\n")
  data <- data[, item_variances >= 2, drop = FALSE]
}

if (ncol(data) == 0) {
  stop("No items with variance remaining after filtering")
}

use_fipc <- !is.na(anchor_params_path) && file.exists(anchor_params_path)
anchor_ids <- character(0)
new_item_ids <- character(0)

if (use_fipc) {
  cat("FIPC mode: loading anchor parameters from", anchor_params_path, "\n")
  anchor_params <- read.csv(anchor_params_path, stringsAsFactors = FALSE)
  if (!all(c("item_id", "difficulty_logit") %in% colnames(anchor_params))) {
    stop("anchor_params.csv must contain item_id and difficulty_logit columns")
  }
  anchor_ids <- anchor_params$item_id[anchor_params$item_id %in% colnames(data)]
  anchor_ids <- anchor_ids[!is.na(anchor_params$difficulty_logit[match(anchor_ids, anchor_params$item_id)])]

  if (!is.na(new_item_ids_path) && file.exists(new_item_ids_path)) {
    raw_ids <- readLines(new_item_ids_path, warn = FALSE)
    raw_ids <- trimws(raw_ids[nzchar(trimws(raw_ids))])
    if (length(raw_ids) == 1 && grepl(",", raw_ids[1])) {
      new_item_ids <- trimws(strsplit(raw_ids[1], ",")[[1]])
    } else {
      new_item_ids <- raw_ids
    }
  } else {
    new_item_ids <- setdiff(colnames(data), anchor_ids)
  }

  cat("Anchors (fixed):", length(anchor_ids), "| New (estimated):", length(new_item_ids), "\n")
  cat("Fitting Rasch (1PL) with fixed anchor difficulties...\n")
} else {
  cat("Fitting Rasch (1PL) model on", ncol(data), "items...\n")
}

start_time <- Sys.time()
use_se <- TRUE

fit_model <- function(pars_table = NULL) {
  if (is.null(pars_table)) {
    mirt(
      data = data,
      model = 1,
      itemtype = 'Rasch',
      verbose = FALSE,
      SE = TRUE
    )
  } else {
    mirt(
      data = data,
      model = 1,
      itemtype = 'Rasch',
      pars = pars_table,
      verbose = FALSE,
      SE = TRUE
    )
  }
}

fit <- NULL

if (use_fipc) {
  pars <- mirt(data = data, model = 1, itemtype = 'Rasch', pars = 'values')

  # Free latent mean for FIPC (default MEAN_1 is fixed at 0).
  pars$est[pars$name == 'MEAN_1'] <- TRUE

  for (aid in anchor_ids) {
    b_val <- anchor_params$difficulty_logit[anchor_params$item_id == aid]
    if (length(b_val) != 1 || is.na(b_val)) next
    # mirt slope-intercept: d = -b for Rasch (a fixed to 1).
    d_val <- -as.numeric(b_val)
    idx <- pars$item == aid & pars$name == 'd'
    if (any(idx)) {
      pars$value[idx] <- d_val
      pars$est[idx] <- FALSE
    }
  }

  fit <- tryCatch(
    fit_model(pars),
    error = function(e) {
      if (grepl("negative eigenvalue|sigma matrix", e$message, ignore.case = TRUE)) {
        cat("Note: Standard errors unavailable. Fitting FIPC with SE = FALSE.\n")
        use_se <<- FALSE
        mirt(
          data = data,
          model = 1,
          itemtype = 'Rasch',
          pars = pars,
          verbose = FALSE,
          SE = FALSE
        )
      } else {
        stop(e)
      }
    }
  )
} else {
  fit <- tryCatch(
    fit_model(NULL),
    error = function(e) {
      if (grepl("negative eigenvalue|sigma matrix", e$message, ignore.case = TRUE)) {
        cat("Note: Standard errors unavailable. Fitting with SE = FALSE.\n")
        use_se <<- FALSE
        mirt(
          data = data,
          model = 1,
          itemtype = 'Rasch',
          verbose = FALSE,
          SE = FALSE
        )
      } else {
        stop(e)
      }
    }
  )
}

elapsed <- as.numeric(difftime(Sys.time(), start_time, units = "secs"))
cat("Model fit complete in", round(elapsed, 2), "seconds\n")

coef_list <- coef(fit, IRTpars = TRUE)

difficulties <- numeric(length(coef_list) - 1)
se_difficulties <- numeric(length(coef_list) - 1)
names(difficulties) <- names(coef_list)[1:(length(coef_list) - 1)]
names(se_difficulties) <- names(coef_list)[1:(length(coef_list) - 1)]

for (i in 1:(length(coef_list) - 1)) {
  item_name <- names(coef_list)[i]
  item_coef <- coef_list[[i]]
  difficulties[item_name] <- item_coef['par', 'b']
  if (use_se && 'CI_2.5' %in% rownames(item_coef) && 'CI_97.5' %in% rownames(item_coef)) {
    ci_lower <- item_coef['CI_2.5', 'b']
    ci_upper <- item_coef['CI_97.5', 'b']
    se_difficulties[item_name] <- (ci_upper - ci_lower) / (2 * 1.96)
  } else {
    se_difficulties[item_name] <- NA
  }
}

item_params <- data.frame(
  item_id = original_items,
  difficulty_logit = NA,
  se_difficulty = NA,
  is_anchor = original_items %in% anchor_ids,
  stringsAsFactors = FALSE
)

estimated_items <- names(difficulties)
item_params[item_params$item_id %in% estimated_items, 'difficulty_logit'] <- difficulties
item_params[item_params$item_id %in% estimated_items, 'se_difficulty'] <- se_difficulties

write.csv(item_params, item_params_path, row.names = FALSE)
cat("Saved item parameters to:", item_params_path, "\n")

# Per-respondent ability (EAP theta) — additive output; item params unchanged.
fs <- fscores(fit, method = "EAP", full.scores = TRUE)
respondent_ids <- rownames(data)
if (is.null(respondent_ids) || length(respondent_ids) == 0 || any(respondent_ids == "")) {
  respondent_ids <- paste0("R", sprintf("%03d", seq_len(nrow(data))))
}
if (nrow(fs) != length(respondent_ids)) {
  stop("fscores row count (", nrow(fs), ") != respondent count (", length(respondent_ids), ")")
}
theta_col <- if ("F1" %in% colnames(fs)) "F1" else colnames(fs)[1]
se_col <- if ("SE_F1" %in% colnames(fs)) "SE_F1" else if (ncol(fs) >= 2) colnames(fs)[2] else NA

ability_df <- data.frame(
  respondent_id = respondent_ids,
  theta = as.numeric(fs[, theta_col]),
  se_theta = if (!is.na(se_col)) as.numeric(fs[, se_col]) else NA_real_,
  stringsAsFactors = FALSE
)

abilities_path <- file.path(dirname(item_params_path), "respondent_abilities.csv")
write.csv(ability_df, abilities_path, row.names = FALSE)
cat("Saved respondent abilities to:", abilities_path, "\n")

fit_stats <- list(
  converged = extract.mirt(fit, 'converged'),
  logLik = as.numeric(logLik(fit)),
  AIC = extract.mirt(fit, 'AIC'),
  BIC = extract.mirt(fit, 'BIC'),
  elapsed_sec = elapsed,
  fipc = use_fipc,
  n_anchors = length(anchor_ids),
  n_new_items = length(new_item_ids)
)

fit_json <- toJSON(fit_stats, pretty = TRUE, auto_unbox = TRUE)
writeLines(fit_json, fit_summary_path)
cat("Saved fit summary to:", fit_summary_path, "\n")

cat("✓ Rasch analysis complete\n")
