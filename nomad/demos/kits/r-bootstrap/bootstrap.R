# A practice run (nomad demos): made-up data, a straight line fitted to it,
# then 20,000 bootstrap fits to see how sure the slope is. Its results mean
# nothing for anyone's research: it shows how an R job runs, what it writes,
# and how it uses one core or several (MC_CORES, set by the job's "8 cores").

cores <- as.integer(Sys.getenv("MC_CORES", "1"))
if (is.na(cores) || cores < 1) cores <- 1

set.seed(42)
n <- 2000
x <- runif(n, 0, 10)
y <- 1.5 + 0.8 * x + rnorm(n, sd = 2)
d <- data.frame(x = x, y = y)

fit <- lm(y ~ x, data = d)
cat(sprintf("Fitted slope: %.3f (the made-up data used 0.8)\n", coef(fit)[["x"]]))

one <- function(i) {
  s <- d[sample.int(n, n, replace = TRUE), ]
  coef(lm(y ~ x, data = s))[["x"]]
}

reps <- 20000
started <- proc.time()
slopes <- if (cores > 1) {
  unlist(parallel::mclapply(seq_len(reps), one, mc.cores = cores))
} else {
  vapply(seq_len(reps), one, numeric(1))
}
took <- (proc.time() - started)[["elapsed"]]
cat(sprintf("%d bootstrap fits on %d core(s) in %.1f s\n", reps, cores, took))

ci <- quantile(slopes, c(0.025, 0.975))
cat(sprintf("Slope, middle 95%% of the bootstrap fits: %.3f to %.3f\n", ci[[1]], ci[[2]]))

write.csv(data.frame(slope = slopes), "bootstrap_slopes.csv", row.names = FALSE)
pdf("bootstrap_slopes.pdf")
hist(slopes, breaks = 60, main = "Bootstrap slopes (a practice run)", xlab = "slope")
invisible(dev.off())
cat("Wrote bootstrap_slopes.csv and bootstrap_slopes.pdf\n")
