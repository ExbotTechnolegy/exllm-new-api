-- Count candidates under the legacy 30-minute rule. Age does not prove nonpayment.
BEGIN READ ONLY;

SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
       count(*) FILTER (
           WHERE status = 'pending'
             AND create_time < floor(extract(epoch FROM transaction_timestamp()))::bigint - 1800
       ) AS stale_pending
FROM top_ups;

COMMIT;
