# One image per probe service: --build-arg PROJECT=<Gateway|Orders|Inventory|Fulfillment>.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG PROJECT
WORKDIR /src
COPY src/${PROJECT}/ ${PROJECT}/
RUN dotnet publish ${PROJECT}/${PROJECT}.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
ARG PROJECT
ENV APP_DLL=${PROJECT}.dll ASPNETCORE_HTTP_PORTS=8080
WORKDIR /app
COPY --from=build /app .
USER app
ENTRYPOINT ["sh", "-c", "exec dotnet \"$APP_DLL\""]
