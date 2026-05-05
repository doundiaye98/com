# Persistance des donnees sur Render

Si les comptes/messages disparaissent apres redeploiement, c'est presque toujours que l'app tourne sans vraie base PostgreSQL persistante.

## 1) Verifier la base PostgreSQL Render

- Ouvre Render -> ton service web `communication-interne`
- Ouvre Render -> ta base `communication-db`
- Verifie que la base est en statut `Available`

## 2) Verifier la variable `DATABASE_URL`

Dans le service web Render (`communication-interne`) :

- Va dans `Environment`
- Verifie que `DATABASE_URL` existe
- La valeur doit venir de la base (`fromDatabase` dans `render.yaml`)
- Elle doit pointer vers PostgreSQL (pas SQLite)

## 3) Variables recommandees en production

Dans `Environment` :

- `SECRET_KEY` : une vraie valeur longue et aleatoire
- `SESSION_HTTPS_ONLY=1`
- `ALLOW_PUBLIC_REGISTRATION` selon ton besoin (`1` ou `0`)

## 4) Redeployer

- Lance un redeploiement du service
- Ouvre `/health`
- Cree un compte test
- Redemarre/redeploie encore une fois puis reconnecte-toi
- Si le compte existe encore, la persistance est OK

## 5) Important: fichiers uploads

Le dossier `storage/avatars` peut etre ephemere sur certains hebergements.
Pour une persistance complete des avatars, utiliser un stockage objet (S3, Cloudflare R2, etc.).
