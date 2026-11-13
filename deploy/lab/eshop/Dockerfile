# One image per eShop service, built from the eShop source tree (which has
# no Dockerfiles; locally it publishes images with the .NET SDK). Used by
# build-images.sh with ACR Tasks, so nothing needs the SDK installed:
#   --build-arg PROJECT=<directory under src/, e.g. Catalog.API>
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
ARG PROJECT
WORKDIR /src
COPY . .
RUN dotnet publish "src/${PROJECT}/${PROJECT}.csproj" -c Release -o /app -p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:10.0
ARG PROJECT
ENV APP_DLL=${PROJECT}.dll
WORKDIR /app
COPY --from=build /app .
# The image's non-root app user (UID 1654, as services.yaml expects).
USER $APP_UID
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec dotnet \"$APP_DLL\""]
