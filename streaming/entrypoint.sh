#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
    driver)
        shift
        spark_args=(--deploy-mode client)
        if [[ -n "${SPARK_DRIVER_BIND_ADDRESS:-}" ]]; then
            spark_args+=(
                --conf "spark.driver.bindAddress=${SPARK_DRIVER_BIND_ADDRESS}"
                --conf "spark.executorEnv.SPARK_DRIVER_POD_IP=${SPARK_DRIVER_BIND_ADDRESS}"
            )
        fi
        exec "${SPARK_HOME}/bin/spark-submit" "${spark_args[@]}" "$@"
        ;;
    executor)
        shift
        exec "${JAVA_HOME}/bin/java" \
            -Xms"${SPARK_EXECUTOR_MEMORY}" \
            -Xmx"${SPARK_EXECUTOR_MEMORY}" \
            -cp "${SPARK_HOME}/jars/*:${SPARK_CONF_DIR}:${PWD}${SPARK_DIST_CLASSPATH:+:${SPARK_DIST_CLASSPATH}}" \
            org.apache.spark.scheduler.cluster.k8s.KubernetesExecutorBackend \
            --driver-url "$SPARK_DRIVER_URL" \
            --executor-id "$SPARK_EXECUTOR_ID" \
            --cores "$SPARK_EXECUTOR_CORES" \
            --app-id "$SPARK_APPLICATION_ID" \
            --hostname "$SPARK_EXECUTOR_POD_IP" \
            --resourceProfileId "$SPARK_RESOURCE_PROFILE_ID" \
            --podName "$SPARK_EXECUTOR_POD_NAME"
        ;;
    *)
        exec "$@"
        ;;
esac
