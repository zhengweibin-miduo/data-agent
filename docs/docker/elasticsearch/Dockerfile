ARG ELASTICSEARCH_VERSION=8.19.17

FROM docker.elastic.co/elasticsearch/elasticsearch:${ELASTICSEARCH_VERSION}

ARG ELASTICSEARCH_VERSION

RUN bin/elasticsearch-plugin install --batch \
    "https://get.infini.cloud/elasticsearch/analysis-ik/${ELASTICSEARCH_VERSION}"
