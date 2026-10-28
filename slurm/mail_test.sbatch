#!/bin/bash
# M6: can HPC3 deliver mail to you? Campaigns mail when they halt (docs/campaigns.md),
# and every halt that mails runs on a CPU node, so this tests the two ways mail could leave:
#   1. this job runs /bin/mail on its CPU node
#   2. Slurm mails at the job's end, from its controller (--mail-type=END)
# Job 96270 (2026-10-26): only 2 arrived; homacs1 has no mail program at all. So campaigns
# send mail the second way (controller/executor.py mail).
#
# Submit on g17 with your address in the environment, never in the repo:
#     export AFAR_MAIL_USER=<you>@sjsu.edu
#     make mail-test
# Then report which of the two messages arrived (check spam too). The job's log,
# logs/afar-mail-test-<jobid>.out, records what the node has for sending mail.
#
#SBATCH --job-name=afar-mail-test
#SBATCH --partition=cpuqs
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:05:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
node=$(hostname -s)
mail_prog="${AFAR_MAIL_PROG:-/bin/mail}"     # a stand-in for dry runs
echo "job=${SLURM_JOB_ID} node=${node} date=$(date -Is)"
: "${AFAR_MAIL_USER:?set AFAR_MAIL_USER before submitting (make mail-test checks this)}"

echo "mail programs on ${node}:"
for p in /bin/mail /usr/bin/mailx /usr/sbin/sendmail; do
    if [[ -e "$p" ]]; then echo "  $p -> $(readlink -f "$p")"; else echo "  $p: missing"; fi
done
echo "mail transfer agent running: $(pgrep -l -x 'master|sendmail|exim|postfix' | tr '\n' ' ' || true)"

if [[ -x "$mail_prog" ]]; then
    printf 'Sent by /bin/mail from %s in Slurm job %s at %s.\nIf this arrived, a halting campaign can mail you directly.\n' \
        "$node" "$SLURM_JOB_ID" "$(date -Is)" \
        | "$mail_prog" -s "AFAR mail test 1 of 2: from compute node ${node} (job ${SLURM_JOB_ID})" "$AFAR_MAIL_USER"
    echo "${mail_prog} exit status: $? (0 means the node accepted it, not that it was delivered)"
else
    echo "${mail_prog} is missing on ${node}, so message 1 can't be sent"
fi
echo "message 2 comes from Slurm when this job ends: subject \"Slurm Job_id=${SLURM_JOB_ID} Name=afar-mail-test Ended ...\""
